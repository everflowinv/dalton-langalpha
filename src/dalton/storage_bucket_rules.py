"""Idempotent S3 / MinIO bucket rules: policy, versioning, and per-role lifecycle (§1.5 #2).

Shared by ``scripts/dalton/configure_minio.sh`` (Everflow & Standalone) and
``scripts/standalone/minio_init.py`` (Standalone Core Batch 3).

Roles:
- ``main`` (e.g. ``dalton-main`` / ``dalton-storage``):
  - Versioning: ``Status=Enabled`` (once enabled, S3/MinIO versioning can only
    be suspended, never fully turned off).
  - Lifecycle rules (scoped by ``<STORAGE_KEY_PREFIX>``):
    1. ``dalton-uploads-expire``: ``<prefix>uploads/`` current versions expire in
       1 or 2 days (default 2).
    2. ``dalton-noncurrent-expire``: ``<prefix>`` noncurrent versions expire after 30 days.
    3. ``dalton-expired-delete-marker``: ``<prefix>`` clean up expired object delete markers.
  - Bucket policy: exactly one statement granting anonymous ``s3:GetObject`` (NOT
    ``s3:GetObjectVersion``) on ``arn:aws:s3:::<bucket>/<prefix>avatars/*``.

- ``secondary`` (e.g. ``dalton-asr`` / ``chat-uploads``):
  - Versioning: ``Status=Enabled``.
  - Lifecycle rules (scoped by ``<STORAGE_KEY_PREFIX>``):
    1. ``dalton-transcription-audio-expire``: ``<prefix>transcriptions/`` current
       versions expire after 7 days (saves space on ASR source audio while chat
       attachments remain permanent).
    2. ``dalton-noncurrent-expire``: ``<prefix>`` noncurrent versions expire after 30 days.
    3. ``dalton-expired-delete-marker``: ``<prefix>`` clean up expired object delete markers.
    Any pre-existing ``dalton-uploads-expire`` rule is explicitly removed (secondary
    buckets may hold legacy transcription audio under ``uploads/{ws}/``).
  - Bucket policy: strictly private (no bucket policy statements).

Merge policy:
- Existing lifecycle rules with IDs outside this module's managed set are
  preserved verbatim and reported back, EXCEPT that on ``secondary`` an
  unmanaged rule equivalent to ``dalton-transcription-audio-expire`` (same
  ``<prefix>transcriptions/`` prefix and same 7-day current-version expiration,
  with no extra filter predicates or extra actions) is replaced by the managed
  rule.
- All policy, versioning, and lifecycle configurations are read back and
  verified; any mismatch raises :class:`BucketRuleVerificationError`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

ROLE_MAIN = "main"
ROLE_SECONDARY = "secondary"
VALID_ROLES = frozenset({ROLE_MAIN, ROLE_SECONDARY})

RULE_ID_UPLOADS_EXPIRE = "dalton-uploads-expire"
RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE = "dalton-transcription-audio-expire"
RULE_ID_NONCURRENT_EXPIRE = "dalton-noncurrent-expire"
RULE_ID_EXPIRED_DELETE_MARKER = "dalton-expired-delete-marker"
MANAGED_RULE_IDS = frozenset(
    {
        RULE_ID_UPLOADS_EXPIRE,
        RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE,
        RULE_ID_NONCURRENT_EXPIRE,
        RULE_ID_EXPIRED_DELETE_MARKER,
    }
)

NONCURRENT_EXPIRE_DAYS = 30
TRANSCRIPTION_AUDIO_PREFIX = "transcriptions/"
TRANSCRIPTION_AUDIO_EXPIRE_DAYS = 7
VALID_UPLOADS_EXPIRE_DAYS = frozenset({1, 2})
AVATARS_POLICY_SID = "DaltonAvatarsPublicRead"


class BucketRuleError(RuntimeError):
    """Base error for bucket rule validation or verification failures."""


class BucketRuleVerificationError(BucketRuleError):
    """Raised when read-back state from S3/MinIO does not match expected rules."""


@dataclass(frozen=True)
class BucketRuleApplyResult:
    """Summary returned by :func:`apply_bucket_rules`."""

    bucket: str
    role: str
    key_prefix: str
    versioning_status: str
    managed_rule_ids: tuple[str, ...]
    preserved_rules: tuple[dict[str, Any], ...]
    removed_secondary_uploads_rule: bool
    policy_verified: bool
    replaced_equivalent_rules: tuple[dict[str, Any], ...] = ()


def normalize_role(role: str) -> str:
    norm = (role or "").strip().lower()
    if norm not in VALID_ROLES:
        raise BucketRuleError(
            f"Invalid bucket role {role!r}; expected one of: main, secondary"
        )
    return norm


def normalize_key_prefix(raw_prefix: str | None) -> str:
    cleaned = (raw_prefix or "").strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


def validate_uploads_expire_days(days: int) -> int:
    if isinstance(days, bool) or not isinstance(days, int) or days not in VALID_UPLOADS_EXPIRE_DAYS:
        raise BucketRuleError(
            f"DALTON_UPLOADS_EXPIRE_DAYS must be 1 or 2, got {days!r}"
        )
    return days


def build_lifecycle_rules(
    *,
    role: str,
    key_prefix: str = "",
    uploads_expire_days: int = 2,
) -> list[dict[str, Any]]:
    """Construct managed S3 lifecycle rules for ``role``."""
    norm_role = normalize_role(role)
    prefix = normalize_key_prefix(key_prefix)
    rules: list[dict[str, Any]] = []

    if norm_role == ROLE_MAIN:
        days = validate_uploads_expire_days(uploads_expire_days)
        rules.append(
            {
                "ID": RULE_ID_UPLOADS_EXPIRE,
                "Status": "Enabled",
                "Filter": {"Prefix": f"{prefix}uploads/"},
                "Expiration": {"Days": days},
            }
        )
    elif norm_role == ROLE_SECONDARY:
        rules.append(
            {
                "ID": RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE,
                "Status": "Enabled",
                "Filter": {"Prefix": f"{prefix}{TRANSCRIPTION_AUDIO_PREFIX}"},
                "Expiration": {"Days": TRANSCRIPTION_AUDIO_EXPIRE_DAYS},
            }
        )

    rules.append(
        {
            "ID": RULE_ID_NONCURRENT_EXPIRE,
            "Status": "Enabled",
            "Filter": {"Prefix": prefix},
            "NoncurrentVersionExpiration": {"NoncurrentDays": NONCURRENT_EXPIRE_DAYS},
        }
    )
    rules.append(
        {
            "ID": RULE_ID_EXPIRED_DELETE_MARKER,
            "Status": "Enabled",
            "Filter": {"Prefix": prefix},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
        }
    )
    return rules


def _rule_prefix(rule: dict[str, Any]) -> str:
    filt = rule.get("Filter")
    if isinstance(filt, dict):
        if "Prefix" in filt and filt["Prefix"] is not None:
            return str(filt["Prefix"])
        and_clause = filt.get("And")
        if isinstance(and_clause, dict) and and_clause.get("Prefix") is not None:
            return str(and_clause["Prefix"])
    if rule.get("Prefix") is not None:
        return str(rule["Prefix"])
    return ""


def _rule_has_only_prefix_filter(rule: dict[str, Any]) -> bool:
    filt = rule.get("Filter")
    if filt is None:
        return True
    if not isinstance(filt, dict):
        return False
    if "And" in filt:
        for key, val in filt.items():
            if key != "And" and val not in (None, "", [], {}):
                return False
        and_clause = filt.get("And")
        if not isinstance(and_clause, dict):
            return False
        for key, val in and_clause.items():
            if key != "Prefix" and val not in (None, "", [], {}):
                return False
        return True
    for key, val in filt.items():
        if key != "Prefix" and val not in (None, "", [], {}):
            return False
    return True


def is_equivalent_transcription_audio_expire_rule(
    rule: dict[str, Any],
    *,
    key_prefix: str = "",
    expire_days: int = TRANSCRIPTION_AUDIO_EXPIRE_DAYS,
) -> bool:
    """Return True iff ``rule`` is semantically equivalent to ``dalton-transcription-audio-expire``."""
    if not isinstance(rule, dict):
        return False
    if rule.get("Status") != "Enabled":
        return False
    if not _rule_has_only_prefix_filter(rule):
        return False
    expected_prefix = f"{normalize_key_prefix(key_prefix)}{TRANSCRIPTION_AUDIO_PREFIX}"
    if _rule_prefix(rule) != expected_prefix:
        return False
    exp = rule.get("Expiration")
    if not isinstance(exp, dict):
        return False
    days = exp.get("Days")
    if isinstance(days, bool) or days != expire_days:
        return False
    if exp.get("Date") is not None or exp.get("ExpiredObjectDeleteMarker"):
        return False
    for extra_action in (
        "NoncurrentVersionExpiration",
        "Transitions",
        "NoncurrentVersionTransitions",
        "AbortIncompleteMultipartUpload",
    ):
        if rule.get(extra_action):
            return False
    return True


def _merge_lifecycle_rules_detailed(
    existing_rules: Sequence[dict[str, Any]],
    *,
    role: str,
    key_prefix: str = "",
    uploads_expire_days: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, list[dict[str, Any]]]:
    norm_role = normalize_role(role)
    prefix = normalize_key_prefix(key_prefix)
    preserved: list[dict[str, Any]] = []
    replaced_equivalent: list[dict[str, Any]] = []
    removed_secondary_uploads = False

    for rule in existing_rules:
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("ID")
        if rule_id in MANAGED_RULE_IDS:
            if norm_role == ROLE_SECONDARY and rule_id == RULE_ID_UPLOADS_EXPIRE:
                removed_secondary_uploads = True
            continue
        if norm_role == ROLE_SECONDARY and is_equivalent_transcription_audio_expire_rule(
            rule,
            key_prefix=prefix,
            expire_days=TRANSCRIPTION_AUDIO_EXPIRE_DAYS,
        ):
            replaced_equivalent.append(dict(rule))
            continue
        preserved.append(dict(rule))

    managed = build_lifecycle_rules(
        role=norm_role,
        key_prefix=prefix,
        uploads_expire_days=uploads_expire_days,
    )
    return [*preserved, *managed], preserved, removed_secondary_uploads, replaced_equivalent


def merge_lifecycle_rules(
    existing_rules: Sequence[dict[str, Any]],
    *,
    role: str,
    key_prefix: str = "",
    uploads_expire_days: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Merge managed rules with existing rules, preserving unmanaged rule IDs.

    On ``secondary``, unmanaged rules equivalent to
    ``dalton-transcription-audio-expire`` (same prefix and expiration days) are
    replaced by the managed rule; all other unmanaged rules are preserved.

    Returns ``(merged_rules, preserved_other_rules, removed_secondary_uploads_rule)``.
    """
    merged, preserved, removed_secondary_uploads, _replaced = (
        _merge_lifecycle_rules_detailed(
            existing_rules,
            role=role,
            key_prefix=key_prefix,
            uploads_expire_days=uploads_expire_days,
        )
    )
    return merged, preserved, removed_secondary_uploads


def verify_lifecycle_rules(
    actual_rules: Sequence[dict[str, Any]],
    *,
    role: str,
    key_prefix: str = "",
    uploads_expire_days: int = 2,
) -> None:
    """Verify read-back lifecycle rules match all managed rules for ``role``."""
    norm_role = normalize_role(role)
    prefix = normalize_key_prefix(key_prefix)
    expected = build_lifecycle_rules(
        role=norm_role,
        key_prefix=prefix,
        uploads_expire_days=uploads_expire_days,
    )
    by_id: dict[str, dict[str, Any]] = {}
    for rule in actual_rules:
        if isinstance(rule, dict) and rule.get("ID"):
            by_id[str(rule["ID"])] = rule

    if norm_role == ROLE_SECONDARY and RULE_ID_UPLOADS_EXPIRE in by_id:
        raise BucketRuleVerificationError(
            f"Secondary bucket must not retain lifecycle rule {RULE_ID_UPLOADS_EXPIRE!r}"
        )
    if norm_role == ROLE_MAIN and RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE in by_id:
        raise BucketRuleVerificationError(
            f"Main bucket must not retain lifecycle rule {RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE!r}"
        )
    if norm_role == ROLE_SECONDARY:
        for rule in actual_rules:
            if not isinstance(rule, dict):
                continue
            rid = rule.get("ID")
            if rid in MANAGED_RULE_IDS:
                continue
            if is_equivalent_transcription_audio_expire_rule(
                rule,
                key_prefix=prefix,
                expire_days=TRANSCRIPTION_AUDIO_EXPIRE_DAYS,
            ):
                raise BucketRuleVerificationError(
                    "Secondary bucket still contains an unmanaged lifecycle rule "
                    f"equivalent to {RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE!r}: ID={rid!r}"
                )

    for exp in expected:
        rid = exp["ID"]
        got = by_id.get(rid)
        if got is None:
            raise BucketRuleVerificationError(
                f"Lifecycle read-back missing managed rule {rid!r}"
            )
        if got.get("Status") != "Enabled":
            raise BucketRuleVerificationError(
                f"Lifecycle rule {rid!r} has Status={got.get('Status')!r}, expected 'Enabled'"
            )
        exp_prefix = _rule_prefix(exp)
        got_prefix = _rule_prefix(got)
        if got_prefix != exp_prefix:
            raise BucketRuleVerificationError(
                f"Lifecycle rule {rid!r} prefix={got_prefix!r}, expected {exp_prefix!r}"
            )

        if "Expiration" in exp:
            got_exp = got.get("Expiration") or {}
            if "Days" in exp["Expiration"]:
                if got_exp.get("Days") != exp["Expiration"]["Days"]:
                    raise BucketRuleVerificationError(
                        f"Lifecycle rule {rid!r} Expiration.Days={got_exp.get('Days')!r}, "
                        f"expected {exp['Expiration']['Days']!r}"
                    )
            if "ExpiredObjectDeleteMarker" in exp["Expiration"]:
                if got_exp.get("ExpiredObjectDeleteMarker") is not True:
                    raise BucketRuleVerificationError(
                        f"Lifecycle rule {rid!r} Expiration.ExpiredObjectDeleteMarker="
                        f"{got_exp.get('ExpiredObjectDeleteMarker')!r}, expected True"
                    )

        if "NoncurrentVersionExpiration" in exp:
            got_nc = got.get("NoncurrentVersionExpiration") or {}
            exp_days = exp["NoncurrentVersionExpiration"]["NoncurrentDays"]
            if got_nc.get("NoncurrentDays") != exp_days:
                raise BucketRuleVerificationError(
                    f"Lifecycle rule {rid!r} NoncurrentVersionExpiration.NoncurrentDays="
                    f"{got_nc.get('NoncurrentDays')!r}, expected {exp_days!r}"
                )


def build_bucket_policy(
    *,
    bucket: str,
    role: str,
    key_prefix: str = "",
) -> dict[str, Any] | None:
    """Build bucket policy document for ``role`` (``None`` for private secondary)."""
    norm_role = normalize_role(role)
    clean_bucket = (bucket or "").strip()
    if not clean_bucket:
        raise BucketRuleError("Bucket name is required")
    if norm_role == ROLE_SECONDARY:
        return None
    prefix = normalize_key_prefix(key_prefix)
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": AVATARS_POLICY_SID,
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{clean_bucket}/{prefix}avatars/*"],
            }
        ],
    }


def _as_str_list(val: Any) -> list[str]:
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(v) for v in val]
    return []


def _principal_is_wildcard(principal: Any) -> bool:
    if principal == "*":
        return True
    if isinstance(principal, dict):
        keys = set(principal.keys())
        if keys != {"AWS"}:
            return False
        aws_vals = _as_str_list(principal.get("AWS"))
        return aws_vals == ["*"]
    return False


def verify_bucket_policy(
    policy_doc: dict[str, Any] | None,
    *,
    bucket: str,
    role: str,
    key_prefix: str = "",
) -> None:
    """Verify read-back bucket policy matches exact access boundary for ``role``."""
    norm_role = normalize_role(role)
    if norm_role == ROLE_SECONDARY:
        if policy_doc is None:
            return
        statements = policy_doc.get("Statement") if isinstance(policy_doc, dict) else None
        if statements:
            raise BucketRuleVerificationError(
                f"Secondary bucket {bucket!r} must have no bucket policy statements, got {statements!r}"
            )
        return

    prefix = normalize_key_prefix(key_prefix)
    expected_resource = f"arn:aws:s3:::{bucket}/{prefix}avatars/*"
    if not isinstance(policy_doc, dict):
        raise BucketRuleVerificationError(
            f"Main bucket {bucket!r} missing expected avatars read-only policy"
        )
    raw_stmts = policy_doc.get("Statement")
    if isinstance(raw_stmts, dict):
        statements = [raw_stmts]
    elif isinstance(raw_stmts, list):
        statements = raw_stmts
    else:
        statements = []

    if len(statements) != 1 or not isinstance(statements[0], dict):
        raise BucketRuleVerificationError(
            f"Main bucket {bucket!r} policy must have exactly 1 statement, got {len(statements)}"
        )

    stmt = statements[0]
    if stmt.get("Effect") != "Allow":
        raise BucketRuleVerificationError(
            f"Main bucket {bucket!r} policy Effect={stmt.get('Effect')!r}, expected 'Allow'"
        )
    if not _principal_is_wildcard(stmt.get("Principal")):
        raise BucketRuleVerificationError(
            f"Main bucket {bucket!r} policy Principal={stmt.get('Principal')!r}, expected '*'"
        )
    actions = [a.lower() for a in _as_str_list(stmt.get("Action"))]
    if actions != ["s3:getobject"]:
        raise BucketRuleVerificationError(
            f"Main bucket {bucket!r} policy Action={stmt.get('Action')!r}, "
            "expected strictly ['s3:GetObject'] (without s3:GetObjectVersion)"
        )
    resources = _as_str_list(stmt.get("Resource"))
    if resources != [expected_resource]:
        raise BucketRuleVerificationError(
            f"Main bucket {bucket!r} policy Resource={resources!r}, expected {[expected_resource]!r}"
        )


def _client_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


def _read_bucket_policy_or_none(client: Any, bucket: str) -> dict[str, Any] | None:
    try:
        resp = client.get_bucket_policy(Bucket=bucket)
    except Exception as exc:
        if _client_error_code(exc) in ("NoSuchBucketPolicy", "404"):
            return None
        raise
    raw = resp.get("Policy") if isinstance(resp, dict) else None
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


def _read_lifecycle_rules_or_empty(client: Any, bucket: str) -> list[dict[str, Any]]:
    try:
        resp = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception as exc:
        if _client_error_code(exc) in ("NoSuchLifecycleConfiguration", "404"):
            return []
        raise
    rules = resp.get("Rules") if isinstance(resp, dict) else None
    return list(rules) if isinstance(rules, list) else []


def apply_bucket_rules(
    client: Any,
    *,
    bucket: str,
    role: str,
    key_prefix: str = "",
    uploads_expire_days: int = 2,
) -> BucketRuleApplyResult:
    """Apply and read-back-verify policy, versioning, and lifecycle on ``bucket``."""
    norm_role = normalize_role(role)
    prefix = normalize_key_prefix(key_prefix)

    client.head_bucket(Bucket=bucket)

    # 1. Bucket policy (delete for secondary; single avatars/* s3:GetObject statement for main)
    expected_policy = build_bucket_policy(
        bucket=bucket, role=norm_role, key_prefix=prefix
    )
    if expected_policy is None:
        try:
            client.delete_bucket_policy(Bucket=bucket)
        except Exception as exc:
            if _client_error_code(exc) not in (
                "NoSuchBucketPolicy",
                "404",
                "MethodNotAllowed",
                "NotImplemented",
            ):
                raise
    else:
        client.put_bucket_policy(
            Bucket=bucket,
            Policy=json.dumps(expected_policy, separators=(",", ":")),
        )
    actual_policy = _read_bucket_policy_or_none(client, bucket)
    verify_bucket_policy(
        actual_policy, bucket=bucket, role=norm_role, key_prefix=prefix
    )

    # 2. Bucket versioning (Enabled; Suspended is upgraded to Enabled)
    current_ver = client.get_bucket_versioning(Bucket=bucket) or {}
    if current_ver.get("Status") != "Enabled":
        client.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
    verified_ver = client.get_bucket_versioning(Bucket=bucket) or {}
    ver_status = str(verified_ver.get("Status") or "")
    if ver_status != "Enabled":
        raise BucketRuleVerificationError(
            f"Bucket {bucket!r} versioning read-back Status={ver_status!r}, expected 'Enabled'"
        )

    # 3. Lifecycle rules (merge with unmanaged IDs, replace equivalent unmanaged
    #    transcription rule & remove uploads-expire on secondary, verify)
    existing_rules = _read_lifecycle_rules_or_empty(client, bucket)
    (
        merged_rules,
        preserved_rules,
        removed_secondary_uploads,
        replaced_equivalent_rules,
    ) = _merge_lifecycle_rules_detailed(
        existing_rules,
        role=norm_role,
        key_prefix=prefix,
        uploads_expire_days=uploads_expire_days,
    )
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={"Rules": merged_rules},
    )
    verified_rules = _read_lifecycle_rules_or_empty(client, bucket)
    verify_lifecycle_rules(
        verified_rules,
        role=norm_role,
        key_prefix=prefix,
        uploads_expire_days=uploads_expire_days,
    )

    managed_ids = tuple(
        r["ID"]
        for r in build_lifecycle_rules(
            role=norm_role,
            key_prefix=prefix,
            uploads_expire_days=uploads_expire_days,
        )
    )
    return BucketRuleApplyResult(
        bucket=bucket,
        role=norm_role,
        key_prefix=prefix,
        versioning_status=ver_status,
        managed_rule_ids=managed_ids,
        preserved_rules=tuple(preserved_rules),
        removed_secondary_uploads_rule=removed_secondary_uploads,
        policy_verified=True,
        replaced_equivalent_rules=tuple(replaced_equivalent_rules),
    )


def _build_s3_client_from_env() -> Any:
    import boto3
    from botocore.config import Config

    endpoint = (
        os.environ.get("STORAGE_ENDPOINT_URL")
        or os.environ.get("S3_ENDPOINT_URL")
        or "http://127.0.0.1:9000"
    ).strip().rstrip("/")
    access_key = (
        os.environ.get("STORAGE_ACCESS_KEY_ID")
        or os.environ.get("MINIO_ROOT_USER")
        or os.environ.get("AWS_ACCESS_KEY_ID")
        or ""
    )
    secret_key = (
        os.environ.get("STORAGE_SECRET_ACCESS_KEY")
        or os.environ.get("MINIO_ROOT_PASSWORD")
        or os.environ.get("AWS_SECRET_ACCESS_KEY")
        or ""
    )
    region = (
        os.environ.get("STORAGE_REGION")
        or os.environ.get("S3_REGION")
        or "us-east-1"
    ).strip() or "us-east-1"
    addressing_style = (
        os.environ.get("STORAGE_ADDRESSING_STYLE", "path").strip().lower() or "path"
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.dalton.storage_bucket_rules",
        description=(
            "Apply and verify S3/MinIO bucket policy, versioning (Enabled; once enabled "
            "versioning can only be suspended, never disabled), and per-role lifecycle rules."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    apply_p = sub.add_parser("apply", help="Apply and verify bucket rules via S3 API.")
    apply_p.add_argument("--bucket", required=True, help="Target bucket name.")
    apply_p.add_argument(
        "--role",
        choices=sorted(VALID_ROLES),
        default=ROLE_MAIN,
        help="Bucket role: main or secondary (default: main).",
    )
    apply_p.add_argument(
        "--prefix",
        default=os.environ.get("STORAGE_KEY_PREFIX", ""),
        help="Optional key prefix (default: STORAGE_KEY_PREFIX).",
    )
    apply_p.add_argument(
        "--uploads-expire-days",
        type=int,
        default=int(os.environ.get("DALTON_UPLOADS_EXPIRE_DAYS", "2")),
        help="Main-bucket uploads/ expiration days (1 or 2, default: 2).",
    )

    args = parser.parse_args(argv)
    if args.command == "apply":
        client = _build_s3_client_from_env()
        result = apply_bucket_rules(
            client,
            bucket=args.bucket.strip(),
            role=args.role,
            key_prefix=args.prefix,
            uploads_expire_days=args.uploads_expire_days,
        )
        print(
            f"[minio-config] Verified bucket policy on '{result.bucket}' "
            f"(role={result.role}, prefix='{result.key_prefix}')."
        )
        print(
            f"[minio-config] Verified Bucket Versioning Status={result.versioning_status} "
            f"on '{result.bucket}'."
        )
        if result.removed_secondary_uploads_rule:
            print(
                f"[minio-config] Removed '{RULE_ID_UPLOADS_EXPIRE}' from secondary bucket "
                f"'{result.bucket}' (preserves uploads/{{ws}}/ transcription sources)."
            )
        for replaced in result.replaced_equivalent_rules:
            print(
                f"[minio-config] Replaced equivalent unmanaged lifecycle rule on '{result.bucket}' "
                f"with '{RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE}': "
                f"ID={replaced.get('ID')!r}"
            )
        for other in result.preserved_rules:
            print(
                f"[minio-config] Preserved unmanaged lifecycle rule on '{result.bucket}': "
                f"ID={other.get('ID')!r} Status={other.get('Status')!r}"
            )
        print(
            f"[minio-config] Verified managed lifecycle rules on '{result.bucket}': "
            f"{', '.join(result.managed_rule_ids)}."
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
