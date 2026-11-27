#!/usr/bin/env python3
"""Standalone Core CLI wrapper for the shared consistent backup & post-restore protocol (§3.1, §6.1).

Thin entrypoint under ``scripts/standalone/``: all protocol logic lives once in
``src.dalton.consistent_backup`` and ``src.dalton.storage_refs`` (no duplicated
version selection, reconciliation, or inflight-state SQL).

Outputs the Standalone Core archive layout:
- ``postgres.dump``
- ``minio-objects.tar.gz`` (containing ``objects/<bucket>/<key>`` and ``_minio_manifest.json``)
- ``asr-jobs.tar.gz`` (terminal snapshot ASR jobs + ``asr_usage.jsonl``, when enabled)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton import consistent_backup, storage_refs
from src.dalton.consistent_backup import (
    BucketArchiveSpec,
    TarGzObjectSink,
    run_consistent_backup,
)
from src.dalton.storage_config import resolve as resolve_storage_config


def _build_s3_client(
    *,
    endpoint_url: str,
    access_key: str,
    secret_key: str,
    region: str = "us-east-1",
    addressing_style: str = "path",
) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def resolve_standalone_bucket_specs(
    env: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[..., Any] = _build_s3_client,
) -> list[BucketArchiveSpec]:
    """Resolve active Standalone Core buckets (supporting mixed external main + built-in aux)."""
    em = os.environ if env is None else env
    specs_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    cfg = resolve_storage_config(env=em, apply_core_defaults=True)
    if cfg.is_storage_enabled and cfg.bucket_name:
        ep = (cfg.endpoint_url or "http://minio:9000").strip().rstrip("/")
        ak = (
            em.get("STORAGE_ACCESS_KEY_ID")
            or em.get("S3_ACCESS_KEY_ID")
            or em.get("MINIO_ROOT_USER")
            or ""
        ).strip()
        sk = (
            em.get("STORAGE_SECRET_ACCESS_KEY")
            or em.get("S3_SECRET_ACCESS_KEY")
            or em.get("MINIO_ROOT_PASSWORD")
            or ""
        ).strip()
        region = (em.get("STORAGE_REGION") or em.get("S3_REGION") or "us-east-1").strip()
        style = (em.get("STORAGE_ADDRESSING_STYLE") or "path").strip().lower()
        prefix = (em.get("STORAGE_KEY_PREFIX") or "").strip().strip("/")
        if ep and ak and sk:
            key = (ep, cfg.bucket_name, prefix)
            specs_by_key[key] = {
                "endpoint": ep,
                "bucket": cfg.bucket_name,
                "prefix": prefix,
                "roles": ["main"],
                "ak": ak,
                "sk": sk,
                "region": region,
                "style": style,
            }

    for role, env_prefix in (
        ("chat_upload", "DALTON_CHAT_UPLOAD_STORAGE"),
        ("asr", "DALTON_ASR_STORAGE"),
    ):
        bucket = (em.get(f"{env_prefix}_BUCKET_NAME") or "").strip()
        if not bucket:
            continue
        ep = (em.get(f"{env_prefix}_ENDPOINT_URL") or "http://minio:9000").strip().rstrip("/")
        ak = (em.get(f"{env_prefix}_ACCESS_KEY_ID") or em.get("MINIO_ROOT_USER") or "").strip()
        sk = (
            em.get(f"{env_prefix}_SECRET_ACCESS_KEY") or em.get("MINIO_ROOT_PASSWORD") or ""
        ).strip()
        region = (em.get(f"{env_prefix}_REGION") or "us-east-1").strip()
        style = (em.get(f"{env_prefix}_ADDRESSING_STYLE") or "path").strip().lower()
        prefix = (em.get(f"{env_prefix}_KEY_PREFIX") or "").strip().strip("/")
        if ep and ak and sk:
            key = (ep, bucket, prefix)
            if key in specs_by_key:
                if role not in specs_by_key[key]["roles"]:
                    specs_by_key[key]["roles"].append(role)
            else:
                specs_by_key[key] = {
                    "endpoint": ep,
                    "bucket": bucket,
                    "prefix": prefix,
                    "roles": [role],
                    "ak": ak,
                    "sk": sk,
                    "region": region,
                    "style": style,
                }

    out: list[BucketArchiveSpec] = []
    for item in specs_by_key.values():
        client = client_factory(
            endpoint_url=item["endpoint"],
            access_key=item["ak"],
            secret_key=item["sk"],
            region=item["region"],
            addressing_style=item["style"],
        )
        out.append(
            BucketArchiveSpec(
                bucket=item["bucket"],
                roles=tuple(item["roles"]),
                key_prefix=item["prefix"],
                s3_client=client,
            )
        )
    return out


def _run_pg_dump(snapshot_id: str, out_path: Path, *, dsn: str) -> None:
    cmd = ["pg_dump", dsn, "-Fc", "--no-owner", "--no-acl", f"--snapshot={snapshot_id}"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as out_fh:
        proc = subprocess.run(cmd, stdout=out_fh, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0 or out_path.stat().st_size == 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"pg_dump --snapshot failed (rc={proc.returncode}): {err}")


def _cmd_backup(args: argparse.Namespace) -> int:
    from src.server.database.pool import open_session_connection

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dump_path = out_dir / "postgres.dump"
    minio_tar = out_dir / "minio-objects.tar.gz"
    asr_dir = Path(args.asr_jobs_dir) if args.asr_jobs_dir else None
    asr_tar = (out_dir / "asr-jobs.tar.gz") if (asr_dir and asr_dir.is_dir()) else None
    dsn = args.pg_dsn or os.getenv("DATABASE_URL") or ""
    if not dsn:
        print("DATABASE_URL or --pg-dsn is required", file=sys.stderr)
        return 2

    specs = resolve_standalone_bucket_specs(os.environ)
    sink = TarGzObjectSink(minio_tar, tmp_dir=out_dir)

    async def _execute():
        conn = await open_session_connection()
        try:
            return await run_consistent_backup(
                conn=conn,
                pg_dump_path=dump_path,
                pg_dump_fn=lambda snap_id, out_p: _run_pg_dump(snap_id, out_p, dsn=dsn),
                bucket_specs=specs,
                object_sink=sink,
                asr_jobs_dir=asr_dir if asr_tar is not None else None,
                asr_archive_path=asr_tar,
            )
        finally:
            await conn.close()

    res = asyncio.run(_execute())
    print(
        json.dumps(
            {
                "snapshot_id": res.snapshot_id,
                "pg_dump_path": str(res.pg_dump_path),
                "minio_archive": str(minio_tar),
                "asr_archive": str(res.asr_archive.archive_path) if res.asr_archive else None,
                "object_count": len(res.manifest_objects),
                "warnings": list(res.warnings),
                "reconcile": res.reconcile_report.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Standalone Core consistent backup & post-restore reconciliation CLI"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_bak = sub.add_parser("backup")
    p_bak.add_argument("--output-dir", required=True)
    p_bak.add_argument("--pg-dsn", default="")
    p_bak.add_argument("--asr-jobs-dir", default="")
    p_bak.set_defaults(func=_cmd_backup)

    p_rec = sub.add_parser("reconcile-restored-inflight-states")
    p_rec.add_argument("--skip-object-check", action="store_true")
    p_rec.set_defaults(
        func=lambda a: consistent_backup.main(
            ["reconcile-restored-inflight-states"]
            + (["--skip-object-check"] if a.skip_object_check else [])
        )
    )

    p_ref = sub.add_parser("reconcile")
    p_ref.add_argument("extra", nargs=argparse.REMAINDER)
    p_ref.set_defaults(func=lambda a: storage_refs.main(["reconcile", *a.extra]))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
