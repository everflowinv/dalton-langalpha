#!/usr/bin/env python3
"""Operational utility for AU1 public share redaction (Scheme A: private buckets).

Supports two operations:
  --warm-shared-slices : Pre-warm raw unredacted ``turn_slices`` for active shared
                         threads (``conversation_threads WHERE is_shared = TRUE``)
                         for the current ``lines_epoch()``.
  --verify             : Resolve all active physical storage buckets via
                         ``resolve_active_storage_targets()`` (upstream primary
                         including legacy ``S3_*``/``AWS_*`` and ``OSS_*``,
                         chat-upload/channel-snapshot bucket, and independent
                         ASR transcription bucket, deduplicated by physical bucket)
                         and issue anonymous HTTP GET probes against both origin
                         endpoints and configured public URL bases / CDN domains
                         for every sensitive prefix, asserting zero anonymous 200s
                         (403 required on existing/probed objects; 403/404 allowed
                         on absent legacy keys).

Note: Scheme B (in-place prefix migration ``--mode prefix-migrate``) is intentionally
omitted per the Scheme A decision (all physical buckets private; avatars-only public
exception on the primary bucket).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import json
import logging
import os
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import quote

from psycopg.rows import dict_row

from src.dalton.share_media import (
    ActiveStorageTarget,
    is_acl_not_supported_error,
    resolve_active_storage_targets,
    storage_private_acl_kwargs,
)
from src.dalton.share_redaction import (
    PUBLIC_SHARE_MAX_TURNS,
    get_public_share_page_budget,
)

logger = logging.getLogger("privatize_shared_thread_objects")


@dataclass(frozen=True)
class ProbeCheckResult:
    bucket: str
    roles: tuple[str, ...]
    prefix: str
    url: str
    status_code: int | None
    ok: bool
    detail: str


def _build_anonymous_probe_urls(target: ActiveStorageTarget, key: str) -> list[str]:
    """Build origin & public/CDN URLs for an anonymous GET check of ``key``."""
    urls: list[str] = []
    quoted = quote(key.lstrip("/"), safe="/")

    # 1. Origin URL
    if target.provider == "oss":
        ep = target.endpoint_url.rstrip("/")
        if ep:
            if "://" in ep:
                scheme, rest = ep.split("://", 1)
                if rest.lower().startswith(f"{target.bucket_name.lower()}."):
                    urls.append(f"{ep}/{quoted}")
                else:
                    urls.append(f"{scheme}://{target.bucket_name}.{rest}/{quoted}")
            else:
                urls.append(f"https://{target.bucket_name}.{ep}/{quoted}")
    else:
        ep = target.endpoint_url.rstrip("/")
        if ep:
            if target.addressing_style == "path":
                urls.append(f"{ep}/{target.bucket_name}/{quoted}")
            else:
                if "://" in ep:
                    scheme, rest = ep.split("://", 1)
                    if rest.lower().startswith(f"{target.bucket_name.lower()}."):
                        urls.append(f"{ep}/{quoted}")
                    else:
                        urls.append(f"{scheme}://{target.bucket_name}.{rest}/{quoted}")
                else:
                    urls.append(f"https://{target.bucket_name}.{ep}/{quoted}")
        else:
            urls.append(
                f"https://{target.bucket_name}.s3.{target.region}.amazonaws.com/{quoted}"
            )

    # 2. Public URL bases (CDN / STORAGE_PUBLIC_URL_BASE / S3_PUBLIC_URL_BASE)
    for base in target.public_url_bases:
        clean_base = base.rstrip("/")
        if not clean_base:
            continue
        u = f"{clean_base}/{quoted}"
        if u not in urls:
            urls.append(u)

    return urls


def _http_anonymous_get_status(url: str, *, timeout_s: float = 10.0) -> tuple[int | None, str]:
    """Perform an unauthenticated HTTP GET with zero credentials and return ``(status, note)``."""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "dalton-share-redaction-verify/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            return int(resp.status), "ok"
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.reason or f"HTTP {exc.code}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _build_s3_client_for_target(target: ActiveStorageTarget) -> Any:
    import boto3
    from botocore.config import Config

    kwargs: dict[str, Any] = {
        "aws_access_key_id": target.access_key_id or None,
        "aws_secret_access_key": target.secret_access_key or None,
        "region_name": target.region or "us-east-1",
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": target.addressing_style or "virtual"},
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    }
    if target.endpoint_url:
        kwargs["endpoint_url"] = target.endpoint_url
    return boto3.client("s3", **kwargs)


def _build_oss_client_for_target(target: ActiveStorageTarget) -> Any:
    import alibabacloud_oss_v2 as oss

    cfg = oss.config.load_default()
    cfg.credentials_provider = oss.credentials.StaticCredentialsProvider(
        target.access_key_id,
        target.secret_access_key,
    )
    cfg.region = target.region or "cn-hangzhou"
    if target.endpoint_url:
        cfg.endpoint = target.endpoint_url
    return oss.Client(cfg)


def _put_temp_probe(target: ActiveStorageTarget, key: str) -> Callable[[], None]:
    """Write a temporary zero-PII probe object to ``target`` (S3 or OSS) and return a delete callable."""
    if target.provider == "oss":
        import alibabacloud_oss_v2 as oss

        client = _build_oss_client_for_target(target)
        acl_kwargs = storage_private_acl_kwargs(
            key, provider="oss", endpoint_url=target.endpoint_url
        )
        try:
            client.put_object(
                oss.PutObjectRequest(
                    bucket=target.bucket_name,
                    key=key,
                    body=b"dalton-au1-verify-probe",
                    content_type="text/plain",
                    **acl_kwargs,
                )
            )
        except Exception as exc:
            if acl_kwargs and is_acl_not_supported_error(exc):
                client.put_object(
                    oss.PutObjectRequest(
                        bucket=target.bucket_name,
                        key=key,
                        body=b"dalton-au1-verify-probe",
                        content_type="text/plain",
                    )
                )
            else:
                raise

        def _cleanup_oss() -> None:
            client.delete_object(
                oss.DeleteObjectRequest(bucket=target.bucket_name, key=key)
            )

        return _cleanup_oss

    client = _build_s3_client_for_target(target)
    acl_kwargs = storage_private_acl_kwargs(
        key, provider="s3", endpoint_url=target.endpoint_url
    )
    params: dict[str, Any] = {
        "Bucket": target.bucket_name,
        "Key": key,
        "Body": b"dalton-au1-verify-probe",
        "ContentType": "text/plain",
        **acl_kwargs,
    }
    try:
        client.put_object(**params)
    except Exception as exc:
        if acl_kwargs and is_acl_not_supported_error(exc):
            params.pop("ACL", None)
            client.put_object(**params)
        else:
            raise

    def _cleanup_s3() -> None:
        client.delete_object(Bucket=target.bucket_name, Key=key)

    return _cleanup_s3


def _find_existing_object_key(target: ActiveStorageTarget, prefix: str) -> str | None:
    """Discover one real existing object under ``prefix`` in S3 or OSS (best effort)."""
    try:
        if target.provider == "oss":
            import alibabacloud_oss_v2 as oss

            client = _build_oss_client_for_target(target)
            res = client.list_objects_v2(
                oss.ListObjectsV2Request(
                    bucket=target.bucket_name, prefix=prefix, max_keys=1
                )
            )
            contents = getattr(res, "contents", None) or ()
            for obj in contents:
                k = getattr(obj, "key", None)
                if k:
                    return str(k)
            return None
        client = _build_s3_client_for_target(target)
        res = client.list_objects_v2(
            Bucket=target.bucket_name, Prefix=prefix, MaxKeys=1
        )
        for obj in res.get("Contents") or ():
            if obj.get("Key"):
                return str(obj["Key"])
    except Exception:
        logger.debug(
            "Could not list existing objects for %s/%s",
            target.bucket_name,
            prefix,
            exc_info=True,
        )
    return None


def verify_storage_targets(
    *,
    targets: list[ActiveStorageTarget] | None = None,
    probe_write: bool = True,
    timeout_s: float = 10.0,
    http_get_fn: Any = None,
    put_probe_fn: Any = None,
    find_existing_fn: Any = None,
    existing_keys_by_prefix: Mapping[str, str] | None = None,
) -> tuple[bool, list[ProbeCheckResult]]:
    """Verify every deduplicated physical bucket & sensitive prefix refuses anonymous GET with HTTP 403.

    Never accepts HTTP 404 as pass: every sensitive prefix is verified against a
    real existing object (a temporary zero-PII probe written & cleaned up for both
    S3 and OSS when ``probe_write=True``, plus any existing object discovered under
    the prefix). Probe cleanup failure also fails verification.
    """
    resolved = resolve_active_storage_targets() if targets is None else targets
    getter = http_get_fn or _http_anonymous_get_status
    writer = put_probe_fn or _put_temp_probe
    finder = find_existing_fn or _find_existing_object_key
    results: list[ProbeCheckResult] = []
    all_ok = True

    if not resolved:
        logger.info("No active object storage targets configured; nothing to verify.")
        return True, []

    run_nonce = uuid.uuid4().hex[:12]

    for target in resolved:
        for prefix in target.sensitive_prefixes:
            keys_to_probe: list[str] = []
            cleanup_fn: Callable[[], None] | None = None
            probe_key = f"{prefix.rstrip('/')}/_dalton_au1_verify_{run_nonce}.txt"

            try:
                if existing_keys_by_prefix and prefix in existing_keys_by_prefix:
                    keys_to_probe.append(str(existing_keys_by_prefix[prefix]))
                else:
                    discovered = finder(target, prefix)
                    if discovered:
                        keys_to_probe.append(discovered)

                if probe_write:
                    cleanup_fn = writer(target, probe_key)
                    if probe_key not in keys_to_probe:
                        keys_to_probe.append(probe_key)

                if not keys_to_probe:
                    all_ok = False
                    results.append(
                        ProbeCheckResult(
                            bucket=target.bucket_name,
                            roles=target.roles,
                            prefix=prefix,
                            url=f"{target.endpoint_url or target.bucket_name}/{prefix}",
                            status_code=None,
                            ok=False,
                            detail="no existing object under prefix and --no-probe-write disabled temporary probe",
                        )
                    )
                    continue

                for obj_key in keys_to_probe:
                    urls = _build_anonymous_probe_urls(target, obj_key)
                    for url in urls:
                        status_code, detail = getter(url, timeout_s=timeout_s)
                        # Strict: only 403 Forbidden proves an existing object cannot be anonymously read.
                        ok = status_code == 403
                        if not ok:
                            all_ok = False
                        results.append(
                            ProbeCheckResult(
                                bucket=target.bucket_name,
                                roles=target.roles,
                                prefix=prefix,
                                url=url,
                                status_code=status_code,
                                ok=ok,
                                detail=detail,
                            )
                        )
            except Exception as exc:
                all_ok = False
                results.append(
                    ProbeCheckResult(
                        bucket=target.bucket_name,
                        roles=target.roles,
                        prefix=prefix,
                        url=f"{target.endpoint_url or target.bucket_name}/{probe_key}",
                        status_code=None,
                        ok=False,
                        detail=f"probe error: {type(exc).__name__}: {exc}",
                    )
                )
            finally:
                if cleanup_fn is not None:
                    try:
                        cleanup_fn()
                    except Exception as cleanup_exc:
                        all_ok = False
                        logger.error(
                            "Failed to clean up verification probe %s in bucket %s: %s",
                            probe_key,
                            target.bucket_name,
                            cleanup_exc,
                        )
                        results.append(
                            ProbeCheckResult(
                                bucket=target.bucket_name,
                                roles=target.roles,
                                prefix=prefix,
                                url=f"{target.endpoint_url or target.bucket_name}/{probe_key}",
                                status_code=None,
                                ok=False,
                                detail=f"probe cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}",
                            )
                        )

    return all_ok, results


async def _ensure_cli_pools_open() -> tuple[Any, Any, bool, bool]:
    """Open the conversation DB pool & LangGraph checkpointer when running standalone CLI."""
    from src.server.app import setup
    from src.server.database.pool import get_or_create_pool
    from src.server.services.history.reader import CheckpointHistoryReader
    from src.server.utils.checkpointer import get_checkpointer, open_checkpointer_pool

    opened_app_pool = False
    opened_checkpointer = False

    conv_pool = get_or_create_pool()
    if conv_pool.closed:
        await conv_pool.open()
        opened_app_pool = True

    cp = setup.checkpointer
    if cp is None:
        cp = get_checkpointer(
            memory_type=os.getenv("MEMORY_DB_TYPE", "postgres"),
            db_host=os.getenv("DB_HOST", "localhost"),
            db_port=os.getenv("DB_PORT", "5432"),
            db_name=os.getenv("DB_NAME", "postgres"),
            db_user=os.getenv("DB_USER", "postgres"),
            db_password=os.getenv("DB_PASSWORD", "postgres"),
        )
        await open_checkpointer_pool(cp)
        setup.checkpointer = cp
        CheckpointHistoryReader.reset_instance()
        opened_checkpointer = True
    return conv_pool, cp, opened_app_pool, opened_checkpointer


async def _close_cli_pools(
    conv_pool: Any,
    cp: Any,
    opened_app_pool: bool,
    opened_checkpointer: bool,
) -> None:
    from src.server.app import setup
    from src.server.services.history.reader import CheckpointHistoryReader
    from src.server.utils.checkpointer import close_checkpointer_pool

    if opened_checkpointer and cp is not None:
        try:
            await close_checkpointer_pool(cp)
        finally:
            setup.checkpointer = None
            CheckpointHistoryReader.reset_instance()
    if opened_app_pool and conv_pool is not None:
        await conv_pool.close()


async def warm_shared_slices(
    *,
    limit_threads: int | None = None,
    concurrency: int = 4,
    manage_pools: bool = True,
) -> dict[str, Any]:
    """Scan ``conversation_threads WHERE is_shared = TRUE`` and warm ``turn_slices``."""
    from src.server.database.conversation.replay_rows import get_replay_thread_data
    from src.server.database.pool import get_db_connection
    from src.server.services.history.replay import lines_epoch, read_replay_page

    conv_pool = None
    cp = None
    opened_app = False
    opened_cp = False
    if manage_pools:
        conv_pool, cp, opened_app, opened_cp = await _ensure_cli_pools_open()

    try:
        epoch = lines_epoch()
        budget = get_public_share_page_budget()

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                sql = (
                    "SELECT conversation_thread_id::text AS thread_id "
                    "FROM conversation_threads "
                    "WHERE is_shared = TRUE "
                    "ORDER BY updated_at DESC"
                )
                params: list[Any] = []
                if limit_threads is not None and limit_threads > 0:
                    sql += " LIMIT %s"
                    params.append(int(limit_threads))
                await cur.execute(sql, params)
                thread_ids = [str(r["thread_id"]) for r in await cur.fetchall()]

        sem = asyncio.Semaphore(max(1, int(concurrency)))
        warmed = 0
        skipped = 0
        failed = 0

        async def _warm_one(tid: str) -> None:
            nonlocal warmed, skipped, failed
            async with sem:
                try:
                    rows = await get_replay_thread_data(tid)
                    if rows is None:
                        skipped += 1
                        return
                    await read_replay_page(
                        rows,
                        limit=PUBLIC_SHARE_MAX_TURNS,
                        budget=budget,
                        persist=True,
                    )
                    warmed += 1
                except Exception as exc:
                    failed += 1
                    logger.warning("Failed warming shared thread %s: %s", tid, exc)

        await asyncio.gather(*(_warm_one(tid) for tid in thread_ids))
        return {
            "lines_epoch": epoch,
            "total_shared_threads": len(thread_ids),
            "warmed": warmed,
            "skipped": skipped,
            "failed": failed,
        }
    finally:
        if manage_pools:
            await _close_cli_pools(conv_pool, cp, opened_app, opened_cp)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AU1 public share redaction operational tool (Scheme A: private buckets)."
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify anonymous GET returns strict HTTP 403 across all deduplicated physical buckets & sensitive prefixes.",
    )
    parser.add_argument(
        "--probe-write",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="With --verify, write & delete a zero-PII probe object per sensitive prefix on S3 and OSS (default: enabled).",
    )
    parser.add_argument(
        "--warm-shared-slices",
        action="store_true",
        help="Pre-warm raw turn_slices for active shared threads (conversation_threads WHERE is_shared = TRUE).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of shared threads to warm with --warm-shared-slices.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Concurrency for --warm-shared-slices (default: 4).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP probe timeout in seconds for --verify (default: 10.0).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output JSON summary.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    if not args.verify and not args.warm_shared_slices:
        print(
            "Specify at least one of --verify or --warm-shared-slices.",
            file=sys.stderr,
        )
        return 2

    summary: dict[str, Any] = {}
    exit_code = 0

    if args.warm_shared_slices:
        warm_res = asyncio.run(
            warm_shared_slices(
                limit_threads=args.limit,
                concurrency=args.concurrency,
            )
        )
        summary["warm_shared_slices"] = warm_res
        if warm_res["failed"] > 0:
            exit_code = 1

    if args.verify:
        ok, checks = verify_storage_targets(
            probe_write=args.probe_write,
            timeout_s=args.timeout,
        )
        summary["verify"] = {
            "ok": ok,
            "total_checks": len(checks),
            "failed_checks": sum(1 for c in checks if not c.ok),
            "checks": [asdict(c) for c in checks],
        }
        if not ok:
            exit_code = 1

    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        if "warm_shared_slices" in summary:
            w = summary["warm_shared_slices"]
            print(
                f"[warm-shared-slices] epoch={w['lines_epoch']} total={w['total_shared_threads']} "
                f"warmed={w['warmed']} skipped={w['skipped']} failed={w['failed']}"
            )
        if "verify" in summary:
            v = summary["verify"]
            print(
                f"[verify] ok={v['ok']} total={v['total_checks']} failed={v['failed_checks']}"
            )
            for chk in v["checks"]:
                mark = "PASS" if chk["ok"] else "FAIL"
                print(
                    f"  [{mark}] bucket={chk['bucket']} roles={','.join(chk['roles'])} "
                    f"prefix={chk['prefix']} status={chk['status_code']} url={chk['url']} ({chk['detail']})"
                )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
