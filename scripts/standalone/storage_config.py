#!/usr/bin/env python3
"""Unified effective object storage configuration resolver for Standalone Core ops scripts.

Mirrors the application's actual configuration loading rules so `backup.sh` and
`restore.sh` agree on which object storage service, bucket(s), and key prefix(es)
are active for:
  - chat_upload (`src/dalton/chat_upload_storage.py` + `docker-compose.core.yml`)
  - asr         (`src/dalton/transcription_storage.py`)
  - general     (`src/utils/storage/__init__.py`, `s3_compatible.py`, `oss_uploader.py`,
                 `key_prefix.py`, and `agent_config.yaml` / `agent_config.<env>.yaml`)

Uses Python standard library only (PyYAML is optional and not required on the host).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*([=:])(.*)$")
_COPY_HEADER_RE = re.compile(
    r"^\s*COPY\s+(?:(?:\"[^\"]+\"|[A-Za-z_][A-Za-z0-9_$]*)\.)?"
    r"(?:\"dalton_chat_file_uploads\"|dalton_chat_file_uploads)\s*"
    r"\(([^)]+)\)\s+FROM\s+stdin;\s*$"
)
_PG_COPY_ESCAPE_RE = re.compile(r"\\(?:([bfnrtv\\])|x([0-9A-Fa-f]{1,2})|([0-7]{1,3})|(.))")
_PG_COPY_SIMPLE_ESCAPES = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
}


def parse_env_file(env_path: Path) -> dict[str, str]:
    """Parse a Docker Compose `.env` file strictly from disk (never reads os.environ)."""
    env_map: dict[str, str] = {}
    if not env_path.is_file():
        return env_map
    with env_path.open(encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _ENV_LINE_RE.match(raw.rstrip("\r\n"))
            if not m:
                continue
            val = m.group(3).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            elif val and val[0] not in ("'", '"'):
                val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
            env_map[m.group(1)] = val
    return env_map


def _strip(val: str | None) -> str:
    return (val or "").strip()


def _norm_ep(url: str | None) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        has_scheme = "://" in raw
        parts = urlsplit(raw if has_scheme else f"//{raw}")
        if parts.hostname is not None:
            host = parts.hostname.lower()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = parts.port
            netloc = f"{host}:{port}" if port is not None else host
            path = parts.path.rstrip("/") if not (parts.query or parts.fragment) else parts.path
            if has_scheme:
                return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, parts.fragment))
            return urlunsplit(("", netloc, path, parts.query, parts.fragment)).lstrip("/")
    except Exception:
        pass
    return raw


def _norm_prefix(raw: str | None) -> str:
    return (raw or "").strip().strip("/")


def parse_compose_service_env(compose_json_path: Path, service_name: str = "backend") -> dict[str, str]:
    """Extract effective environment variables for ``service_name`` from ``docker compose config --format json``."""
    data = json.loads(compose_json_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("compose config JSON root must be a JSON object")
    services = data.get("services")
    if not isinstance(services, dict) or service_name not in services:
        raise ValueError(f"compose config JSON does not define service {service_name!r}")
    svc = services[service_name]
    if not isinstance(svc, dict):
        raise ValueError(f"compose config service {service_name!r} must be a JSON object")
    raw_env = svc.get("environment")
    env_map: dict[str, str] = {}
    if raw_env is None:
        return env_map
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            if v is None:
                continue
            env_map[str(k)] = str(v)
    elif isinstance(raw_env, list):
        for item in raw_env:
            if not isinstance(item, str) or "=" not in item:
                continue
            k, v = item.split("=", 1)
            env_map[k] = v
    else:
        raise ValueError(f"compose config service {service_name!r} environment has unexpected type")
    return env_map


def _extract_yaml_storage_provider_file(yaml_path: Path) -> str | None:
    """Read `storage.provider` from a YAML file without requiring PyYAML."""
    if not yaml_path.is_file():
        return None
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        yaml = None

    if yaml is not None:
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict):
            storage = data.get("storage")
            if isinstance(storage, dict):
                val = storage.get("provider")
                if val is not None and str(val).strip() != "":
                    return str(val).strip().lower()
        return None

    in_storage = False
    storage_indent = 0
    for raw in yaml_path.read_text(encoding="utf-8").split("\n"):
        raw = raw.rstrip("\r")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            in_storage = stripped == "storage:" or stripped.startswith("storage: ")
            storage_indent = indent
            continue
        if in_storage and indent > storage_indent:
            if stripped.startswith("provider:"):
                val = stripped[len("provider:") :].strip()
                if val.startswith(("'", '"')) and len(val) >= 2:
                    quote = val[0]
                    end = val.find(quote, 1)
                    if end != -1:
                        val = val[1:end]
                else:
                    val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
                if val and val.lower() not in ("null", "~"):
                    return val.lower()
                return None
    return None


def resolve_yaml_storage_provider(
    work_dir: Path | None,
    env_map: dict[str, str],
) -> tuple[str | None, bool]:
    """Return `(storage.provider, yaml_found)` mirroring `src/ptc_agent/config/file_utils.py`."""
    base_path: Path | None = None
    ptc_cfg = _strip(env_map.get("PTC_CONFIG_FILE"))
    if ptc_cfg:
        cand = Path(ptc_cfg)
        if not cand.is_absolute() and work_dir is not None:
            cand = work_dir / cand
        if cand.is_file():
            base_path = cand
    if base_path is None and work_dir is not None:
        cand = work_dir / "agent_config.yaml"
        if cand.is_file():
            base_path = cand
    if base_path is None:
        return None, False

    provider = _extract_yaml_storage_provider_file(base_path)
    app_env = _strip(env_map.get("APP_ENV")) or _strip(env_map.get("DALTON_PROFILE")) or "core"
    overlay_path = base_path.with_name(f"{base_path.stem}.{app_env}{base_path.suffix}")
    if overlay_path.is_file():
        overlay_provider = _extract_yaml_storage_provider_file(overlay_path)
        if overlay_provider is not None:
            provider = overlay_provider
    return provider, True


def resolve_effective_storage(
    env_map: dict[str, str],
    work_dir: Path | None = None,
    *,
    apply_compose_defaults: bool = True,
    missing_yaml_default_provider: str = "s3",
) -> dict[str, Any]:
    """Resolve effective object storage targets for chat_upload, asr, and general storage."""
    # 1. ASR / Transcription (`src/dalton/transcription_storage.py`)
    asr_ep = _norm_ep(env_map.get("DALTON_ASR_STORAGE_ENDPOINT_URL"))
    asr_bucket = _strip(env_map.get("DALTON_ASR_STORAGE_BUCKET_NAME"))
    asr_ak = _strip(env_map.get("DALTON_ASR_STORAGE_ACCESS_KEY_ID"))
    asr_sk = _strip(env_map.get("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY"))
    asr_prefix = _norm_prefix(env_map.get("DALTON_ASR_STORAGE_KEY_PREFIX"))

    asr_target: dict[str, Any] | None = None
    if asr_ep or (asr_bucket and asr_ak and asr_sk):
        asr_target = {
            "service_type": "s3",
            "service_id": f"s3:{asr_ep}" if asr_ep else "s3:asr-unset-endpoint",
            "endpoint": asr_ep,
            "bucket": asr_bucket,
            "key_prefix": asr_prefix,
        }

    # 2. Chat upload (`src/dalton/chat_upload_storage.py` + `docker-compose.core.yml`)
    raw_chat_ep = _norm_ep(env_map.get("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL"))
    raw_chat_bucket = _strip(env_map.get("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"))
    raw_chat_ak = _strip(env_map.get("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID"))
    raw_chat_sk = _strip(env_map.get("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY"))
    minio_user = _strip(env_map.get("MINIO_ROOT_USER"))
    minio_pass = _strip(env_map.get("MINIO_ROOT_PASSWORD"))

    chat_target: dict[str, Any] | None = None
    if apply_compose_defaults:
        eff_chat_ak = raw_chat_ak or minio_user
        eff_chat_sk = raw_chat_sk or minio_pass
        has_dedicated = bool((eff_chat_ak and eff_chat_sk) or raw_chat_ep or raw_chat_bucket)
        if has_dedicated:
            chat_ep = raw_chat_ep or "http://minio:9000"
            chat_bucket = raw_chat_bucket or "chat-uploads"
            chat_prefix = _norm_prefix(env_map.get("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX"))
            chat_target = {
                "service_type": "s3",
                "service_id": f"s3:{chat_ep}",
                "endpoint": chat_ep,
                "bucket": chat_bucket,
                "key_prefix": chat_prefix,
            }
        elif asr_target is not None:
            chat_target = dict(asr_target)
    else:
        has_dedicated = bool(raw_chat_ep and raw_chat_bucket and raw_chat_ak and raw_chat_sk)
        if has_dedicated:
            chat_prefix = _norm_prefix(env_map.get("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX"))
            chat_target = {
                "service_type": "s3",
                "service_id": f"s3:{raw_chat_ep}",
                "endpoint": raw_chat_ep,
                "bucket": raw_chat_bucket,
                "key_prefix": chat_prefix,
            }
        elif asr_target is not None:
            chat_target = dict(asr_target)

    # 3. General storage (`src/utils/storage/__init__.py`)
    yaml_provider, yaml_found = resolve_yaml_storage_provider(work_dir, env_map)
    env_storage_provider = _strip(env_map.get("STORAGE_PROVIDER")).lower()
    if yaml_found:
        raw_provider = yaml_provider if yaml_provider else (env_storage_provider or "none")
    else:
        raw_provider = env_storage_provider if env_storage_provider else missing_yaml_default_provider

    general_prefix = _norm_prefix(env_map.get("STORAGE_KEY_PREFIX"))
    general_target: dict[str, Any] | None = None
    if raw_provider == "none":
        general_target = None
    elif raw_provider == "oss":
        oss_bucket = _strip(env_map.get("OSS_BUCKET_NAME"))
        if oss_bucket:
            oss_ep = _norm_ep(env_map.get("OSS_ENDPOINT"))
            oss_region = _strip(env_map.get("OSS_REGION")).lower()
            oss_id = oss_ep or (f"default-oss:{oss_region}" if oss_region else "default-oss")
            general_target = {
                "service_type": "oss",
                "service_id": f"oss:{oss_id}",
                "endpoint": oss_ep,
                "bucket": oss_bucket,
                "key_prefix": general_prefix,
                "provider": "oss",
            }
    else:
        s3_bucket = _strip(env_map.get("STORAGE_BUCKET_NAME")) or _strip(env_map.get("S3_BUCKET_NAME"))
        if s3_bucket:
            s3_ep = _norm_ep(env_map.get("STORAGE_ENDPOINT_URL") or env_map.get("S3_ENDPOINT_URL"))
            s3_region = (
                _strip(env_map.get("STORAGE_REGION"))
                or _strip(env_map.get("S3_REGION"))
                or "us-east-1"
            ).lower()
            service_id = f"s3:{s3_ep}" if s3_ep else f"s3:aws-s3-default:{s3_region}"
            general_target = {
                "service_type": "s3",
                "service_id": service_id,
                "endpoint": s3_ep,
                "bucket": s3_bucket,
                "key_prefix": general_prefix,
                "provider": raw_provider,
            }

    targets: dict[str, dict[str, Any]] = {}
    if chat_target is not None:
        targets["chat_upload"] = chat_target
    if asr_target is not None:
        targets["asr"] = asr_target
    if general_target is not None:
        targets["general"] = general_target

    distinct_services = sorted({t["service_id"] for t in targets.values()})
    conflicts: list[str] = []
    if len(distinct_services) > 1:
        details = ", ".join(
            f"{name}={t['service_id']} (bucket={t.get('bucket') or '<unset>'})"
            for name, t in targets.items()
        )
        conflicts.append(details)

    primary_bucket = ""
    primary_key_prefix = ""
    if "chat_upload" in targets:
        primary_bucket = str(targets["chat_upload"].get("bucket") or "")
        primary_key_prefix = str(targets["chat_upload"].get("key_prefix") or "")
    elif targets:
        first_target = next(iter(targets.values()))
        primary_bucket = str(first_target.get("bucket") or "")
        primary_key_prefix = str(first_target.get("key_prefix") or "")

    return {
        "any_configured": bool(targets),
        "yaml_found": yaml_found,
        "yaml_provider": yaml_provider,
        "effective_general_provider": raw_provider,
        "service_ids": distinct_services,
        "single_service_ok": len(distinct_services) <= 1,
        "conflicts": conflicts,
        "primary_bucket": primary_bucket,
        "primary_key_prefix": primary_key_prefix,
        "targets": {
            name: {
                "service_type": t["service_type"],
                "service_id": t["service_id"],
                "endpoint": t["endpoint"],
                "bucket": t["bucket"],
                "key_prefix": t["key_prefix"],
            }
            for name, t in targets.items()
        },
    }


def _extract_backup_targets(
    minio_manifest: dict[str, Any] | None,
    backup_manifest: dict[str, Any] | None,
) -> dict[str, dict[str, str]]:
    """Extract normalized `{subsystem: {bucket, key_prefix, [endpoint]}}` from backup manifests."""
    raw_targets: Any = None
    if isinstance(minio_manifest, dict) and isinstance(minio_manifest.get("targets"), dict):
        raw_targets = minio_manifest["targets"]
    elif isinstance(backup_manifest, dict):
        minio_sec = backup_manifest.get("minio")
        if isinstance(minio_sec, dict) and isinstance(minio_sec.get("targets"), dict):
            raw_targets = minio_sec["targets"]

    if isinstance(raw_targets, dict) and raw_targets:
        out: dict[str, dict[str, str]] = {}
        for name, entry in raw_targets.items():
            if not isinstance(entry, dict):
                continue
            rec: dict[str, str] = {
                "bucket": _strip(str(entry.get("bucket") or "")),
                "key_prefix": _norm_prefix(str(entry.get("key_prefix") or "")),
            }
            if "endpoint" in entry:
                rec["endpoint"] = _norm_ep(str(entry.get("endpoint") or ""))
            out[str(name)] = rec
        return out

    # Backward-compatible fallback for backups produced before `targets` was recorded
    fallback: dict[str, dict[str, str]] = {}
    primary_bucket = ""
    primary_prefix = ""
    var_buckets: dict[str, str] = {}
    if isinstance(minio_manifest, dict):
        primary_bucket = _strip(str(minio_manifest.get("primary_bucket") or ""))
        primary_prefix = _norm_prefix(str(minio_manifest.get("primary_key_prefix") or ""))
        vb = minio_manifest.get("var_buckets")
        if isinstance(vb, dict):
            var_buckets = {str(k): _strip(str(v)) for k, v in vb.items() if _strip(str(v))}
    if not primary_bucket and isinstance(backup_manifest, dict):
        minio_sec = backup_manifest.get("minio")
        if isinstance(minio_sec, dict):
            primary_bucket = _strip(str(minio_sec.get("primary_bucket") or ""))
            primary_prefix = _norm_prefix(str(minio_sec.get("primary_key_prefix") or ""))

    if primary_bucket:
        fallback["chat_upload"] = {"bucket": primary_bucket, "key_prefix": primary_prefix}
    asr_b = var_buckets.get("DALTON_ASR_STORAGE_BUCKET_NAME", "")
    if asr_b:
        fallback["asr"] = {"bucket": asr_b, "key_prefix": ""}
    gen_b = (
        var_buckets.get("STORAGE_BUCKET_NAME")
        or var_buckets.get("S3_BUCKET_NAME")
        or var_buckets.get("OSS_BUCKET_NAME")
        or ""
    )
    if gen_b:
        fallback["general"] = {"bucket": gen_b, "key_prefix": ""}
    return fallback


def compare_backup_with_target(
    target_cfg: dict[str, Any],
    minio_manifest: dict[str, Any] | None,
    backup_manifest: dict[str, Any] | None,
    *,
    source_label: str = "target .env",
    ref_env_cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Compare backup manifest bucket names, endpoints, and key prefixes against effective target config."""
    backup_targets = _extract_backup_targets(minio_manifest, backup_manifest)
    if not backup_targets:
        return []

    target_targets: dict[str, dict[str, Any]] = target_cfg.get("targets") or {}
    ref_targets: dict[str, dict[str, Any]] = (ref_env_cfg or {}).get("targets") or {}
    mismatches: list[str] = []
    all_names = sorted(set(backup_targets.keys()) | set(target_targets.keys()))

    def _ref_note(name: str, field: str, actual_val: str) -> str:
        if ref_env_cfg is None:
            return ""
        ref_t = ref_targets.get(name)
        if not isinstance(ref_t, dict):
            return ""
        ref_val = str(ref_t.get(field) or "")
        if ref_val != actual_val:
            return f" (.env reference has {field}={ref_val!r}, overridden by Compose config)"
        return ""

    for name in all_names:
        b = backup_targets.get(name)
        t = target_targets.get(name)
        if b is None and t is not None:
            mismatches.append(
                f"{name}: backup had no {name} object storage, but {source_label} configures "
                f"endpoint={t.get('endpoint')!r}, bucket={t.get('bucket')!r}, key_prefix={t.get('key_prefix')!r}"
            )
            continue
        if b is not None and t is None:
            ep_part = f"endpoint={b.get('endpoint')!r}, " if "endpoint" in b else ""
            mismatches.append(
                f"{name}: backup expects {ep_part}bucket={b.get('bucket')!r}, key_prefix={b.get('key_prefix')!r}, "
                f"but {source_label} does not configure {name} storage"
            )
            continue
        assert b is not None and t is not None
        b_bucket = str(b.get("bucket") or "")
        t_bucket = str(t.get("bucket") or "")
        b_prefix = str(b.get("key_prefix") or "")
        t_prefix = str(t.get("key_prefix") or "")
        if b_bucket != t_bucket:
            mismatches.append(
                f"{name} bucket mismatch: backup={b_bucket!r} != {source_label}={t_bucket!r}"
                f"{_ref_note(name, 'bucket', t_bucket)}"
            )
        if "endpoint" in b:
            b_ep = _norm_ep(b.get("endpoint"))
            t_ep = _norm_ep(str(t.get("endpoint") or ""))
            if b_ep != t_ep:
                mismatches.append(
                    f"{name} endpoint mismatch: backup={b_ep!r} != {source_label}={t_ep!r}"
                    f"{_ref_note(name, 'endpoint', t_ep)}"
                )
        if b_prefix != t_prefix:
            mismatches.append(
                f"{name} key_prefix mismatch: backup={b_prefix!r} != {source_label}={t_prefix!r}"
                f"{_ref_note(name, 'key_prefix', t_prefix)}"
            )
    return mismatches


def _unescape_pg_copy_field(raw: str) -> str | None:
    """Decode one PostgreSQL COPY text-format column (`\\N` -> None)."""
    if raw == r"\N":
        return None

    def _repl(m: re.Match[str]) -> str:
        simple, hex_part, oct_part, other = m.groups()
        if simple is not None:
            return _PG_COPY_SIMPLE_ESCAPES[simple]
        if hex_part is not None:
            return chr(int(hex_part, 16))
        if oct_part is not None:
            return chr(int(oct_part, 8))
        return other or ""

    return _PG_COPY_ESCAPE_RE.sub(_repl, raw)


def parse_chat_uploads_copy_sql(copy_sql: str) -> list[dict[str, str | None]]:
    """Parse `COPY public.dalton_chat_file_uploads (...) FROM stdin;` rows from `pg_restore --data-only`."""
    columns: list[str] | None = None
    in_copy = False
    found_block = False
    closed_block = False
    rows: list[dict[str, str | None]] = []

    for raw_line in copy_sql.split("\n"):
        line = raw_line.rstrip("\r")
        if not in_copy:
            m = _COPY_HEADER_RE.match(line)
            if m:
                columns = [c.strip().strip('"') for c in m.group(1).split(",") if c.strip()]
                missing_cols = [
                    req for req in ("upload_id", "object_key", "state", "cleaned_at") if req not in columns
                ]
                if missing_cols:
                    raise ValueError(
                        f"dalton_chat_file_uploads COPY header missing required column(s): {', '.join(missing_cols)}"
                    )
                in_copy = True
                found_block = True
                closed_block = False
            continue

        if line == r"\.":
            in_copy = False
            closed_block = True
            continue

        assert columns is not None
        parts = line.split("\t")
        if len(parts) != len(columns):
            raise ValueError(
                f"Malformed COPY row in dalton_chat_file_uploads: expected {len(columns)} columns, got {len(parts)}"
            )
        row = {col: _unescape_pg_copy_field(val) for col, val in zip(columns, parts, strict=True)}
        rows.append(row)

    if not found_block:
        raise ValueError("Missing COPY block for dalton_chat_file_uploads in pg_restore output")
    if in_copy or not closed_block:
        raise ValueError("Unterminated COPY block for dalton_chat_file_uploads in pg_restore output")
    return rows


def verify_backup_chat_upload_refs(
    copy_sql: str,
    minio_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify every completed (non-`uploading`, uncleaned) chat upload in the DB snapshot exists in the MinIO archive."""
    rows = parse_chat_uploads_copy_sql(copy_sql)

    targets = minio_manifest.get("targets") if isinstance(minio_manifest.get("targets"), dict) else {}
    chat_target = targets.get("chat_upload") if isinstance(targets.get("chat_upload"), dict) else {}
    bucket = _strip(str(chat_target.get("bucket") or minio_manifest.get("primary_bucket") or ""))
    key_prefix = _norm_prefix(
        str(chat_target.get("key_prefix") or minio_manifest.get("primary_key_prefix") or "")
    )

    archived_pairs: set[tuple[str, str]] = set()
    for item in minio_manifest.get("objects") or []:
        if isinstance(item, dict):
            b = _strip(str(item.get("bucket") or ""))
            k = str(item.get("key") or "")
            if b and k:
                archived_pairs.add((b, k))

    checked = 0
    skipped_uploading = 0
    missing: list[str] = []
    for row in rows:
        if row.get("cleaned_at") is not None:
            continue
        state = _strip(row.get("state"))
        obj_key = _strip(row.get("object_key"))
        upload_id = _strip(row.get("upload_id")) or "<unknown>"
        if state == "uploading":
            skipped_uploading += 1
            continue
        if state not in ("ready", "completing") or not obj_key:
            continue
        full_key = obj_key.lstrip("/")
        if key_prefix and not full_key.startswith(f"{key_prefix}/"):
            full_key = f"{key_prefix}/{full_key}"
        checked += 1
        if not bucket or (bucket, full_key) not in archived_pairs:
            missing.append(f"chat_upload:{upload_id} (state={state}) -> {bucket or '<unset>'}/{full_key}")

    return {
        "bucket": bucket,
        "key_prefix": key_prefix,
        "checked": checked,
        "skipped_uploading": skipped_uploading,
        "missing": missing,
    }


def _resolve_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
    """Resolve effective storage config: compose config JSON is authoritative, `.env` is auxiliary reference."""
    work_dir = Path(args.work_dir) if args.work_dir else None
    env_ref_cfg: dict[str, Any] | None = None
    if getattr(args, "env_file", ""):
        env_map = parse_env_file(Path(args.env_file))
        env_ref_cfg = resolve_effective_storage(env_map, work_dir, apply_compose_defaults=True)

    compose_json = getattr(args, "compose_config_json", "")
    if compose_json:
        service_name = getattr(args, "service", "backend") or "backend"
        svc_env = parse_compose_service_env(Path(compose_json), service_name=service_name)
        cfg = resolve_effective_storage(svc_env, work_dir, apply_compose_defaults=False)
        return cfg, f"{service_name} compose config", env_ref_cfg

    if env_ref_cfg is None:
        raise ValueError("Either --compose-config-json or --env-file must be provided")
    return env_ref_cfg, "target .env", None


def _cmd_check_env(args: argparse.Namespace) -> int:
    prefix = f"[{args.caller}] " if args.caller else ""
    try:
        cfg, _source_label, _ref_cfg = _resolve_from_args(args)
    except Exception as exc:
        print(f"{prefix}ERROR: Failed to resolve effective storage configuration: {exc}", file=sys.stderr)
        return 1

    if not cfg["single_service_ok"]:
        print(
            f"{prefix}ERROR: Multiple object storage services detected: {'; '.join(cfg['conflicts'])}",
            file=sys.stderr,
        )
        print(
            f"{prefix}ERROR: v1 only supports a single object storage service; configure all application buckets on the same storage service.",
            file=sys.stderr,
        )
        return 1

    if args.require_none_for_skip_minio and cfg["any_configured"]:
        active_desc = ", ".join(
            f"{name} ({t['service_id']}, bucket={t.get('bucket') or '<unset>'})"
            for name, t in cfg["targets"].items()
        )
        print(
            f"{prefix}ERROR: --skip-minio is only allowed when the application has no object storage configured "
            f"(active object storage detected: {active_desc}).",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(cfg, ensure_ascii=False))
    return 0


def _cmd_compare_backup(args: argparse.Namespace) -> int:
    prefix = f"[{args.caller}] " if args.caller else ""
    try:
        cfg, source_label, ref_env_cfg = _resolve_from_args(args)
    except Exception as exc:
        print(f"{prefix}ERROR: Failed to resolve effective storage configuration: {exc}", file=sys.stderr)
        return 1

    minio_manifest: dict[str, Any] | None = None
    if args.minio_manifest and Path(args.minio_manifest).is_file():
        minio_manifest = json.loads(Path(args.minio_manifest).read_text(encoding="utf-8"))

    backup_manifest: dict[str, Any] | None = None
    if args.backup_manifest and Path(args.backup_manifest).is_file():
        backup_manifest = json.loads(Path(args.backup_manifest).read_text(encoding="utf-8"))

    mismatches = compare_backup_with_target(
        cfg,
        minio_manifest,
        backup_manifest,
        source_label=source_label,
        ref_env_cfg=ref_env_cfg,
    )
    if mismatches:
        print(
            f"{prefix}ERROR: Object storage configuration (bucket/endpoint/key-prefix) mismatch between backup manifest and {source_label}:",
            file=sys.stderr,
        )
        for item in mismatches:
            print(f"{prefix}ERROR:   - {item}", file=sys.stderr)
        print(
            f"{prefix}ERROR: Update Compose override files or .env so backend's effective bucket names, endpoints, and key prefixes match the backup, or pass --replace-env (with --env-passphrase-file) to use the configuration from the backup (请修正 Compose 覆盖文件或先改 .env 让两者一致，或者用 --replace-env 使用备份里的配置).",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_verify_backup_refs(args: argparse.Namespace) -> int:
    prefix = f"[{args.caller}] " if args.caller else ""
    try:
        copy_sql = (
            Path(args.copy_sql).read_text(encoding="utf-8")
            if args.copy_sql
            else sys.stdin.read()
        )
        minio_manifest = json.loads(Path(args.minio_manifest).read_text(encoding="utf-8"))
        result = verify_backup_chat_upload_refs(copy_sql, minio_manifest)
    except Exception as exc:
        print(
            f"{prefix}ERROR: Failed to verify database snapshot chat upload references against MinIO archive: {exc}",
            file=sys.stderr,
        )
        return 1

    missing: list[str] = result["missing"]
    checked = int(result["checked"])
    skipped_uploading = int(result["skipped_uploading"])
    if missing:
        print(
            f"{prefix}ERROR: {len(missing)}/{checked} completed chat upload object(s) in the PostgreSQL snapshot are missing from the MinIO archive:",
            file=sys.stderr,
        )
        for item in missing:
            print(f"{prefix}ERROR:   missing: {item}", file=sys.stderr)
        print(
            f"{prefix}ERROR: 可能是备份期间有清理任务运行，请重新备份。",
            file=sys.stderr,
        )
        return 1

    print(
        f"{prefix}OK: Verified {checked} completed chat upload object reference(s) in archive "
        f"(skipped {skipped_uploading} in-progress 'uploading' record(s))."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone Core effective object storage config helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check-env")
    p_check.add_argument("--env-file", default="")
    p_check.add_argument("--compose-config-json", default="")
    p_check.add_argument("--service", default="backend")
    p_check.add_argument("--work-dir", default="")
    p_check.add_argument("--caller", default="")
    p_check.add_argument("--require-none-for-skip-minio", action="store_true")
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=_cmd_check_env)

    p_cmp = sub.add_parser("compare-backup")
    p_cmp.add_argument("--env-file", default="")
    p_cmp.add_argument("--compose-config-json", default="")
    p_cmp.add_argument("--service", default="backend")
    p_cmp.add_argument("--work-dir", default="")
    p_cmp.add_argument("--minio-manifest", default="")
    p_cmp.add_argument("--backup-manifest", default="")
    p_cmp.add_argument("--caller", default="")
    p_cmp.set_defaults(func=_cmd_compare_backup)

    p_ref = sub.add_parser("verify-backup-refs")
    p_ref.add_argument("--copy-sql", default="")
    p_ref.add_argument("--minio-manifest", required=True)
    p_ref.add_argument("--caller", default="")
    p_ref.set_defaults(func=_cmd_verify_backup_refs)

    ns = parser.parse_args(argv)
    return int(ns.func(ns))


if __name__ == "__main__":
    raise SystemExit(main())
