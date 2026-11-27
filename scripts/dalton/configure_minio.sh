#!/usr/bin/env bash
# Idempotently provision, harden, and verify a Dalton MinIO / S3-compatible bucket:
#   1. Create the bucket if it does not exist.
#   2. Reset anonymous access (`mc anonymous set none`), then apply role-specific
#      bucket policy via `src/dalton/storage_bucket_rules.py`:
#      - `main`: anonymous read-only `s3:GetObject` (no `s3:GetObjectVersion`) on
#        `<STORAGE_KEY_PREFIX>avatars/*` only; read-back verified as a single statement.
#      - `secondary`: strictly private (no bucket policy); read-back verified.
#   3. Enable Bucket Versioning (`Status=Enabled`) on both roles and read-back verify.
#      NOTE: Once Bucket Versioning is enabled on a bucket, it can only be suspended
#      (`Suspended`) and cannot be turned off completely.
#   4. Configure and read-back verify per-role S3 Lifecycle rules (merging with and
#      preserving any unmanaged rule IDs):
#      - `main`: `dalton-uploads-expire` (`<prefix>uploads/` 1-2d),
#        `dalton-noncurrent-expire` (30d), `dalton-expired-delete-marker`.
#      - `secondary`: `dalton-noncurrent-expire` (30d) and
#        `dalton-expired-delete-marker` only (removes any `dalton-uploads-expire`
#        so legitimate `uploads/{ws}/` transcription audio is preserved).
#   5. Verify MinIO `api.stale_uploads_expiry` (default 24h) and
#      `api.stale_uploads_cleanup_interval` (default 6h).
#   6. Verify CORS when required (`secondary`, or `main` with
#      `STORAGE_BROWSER_UPLOADS=true`); skipped on `main` when browser uploads are off.
#
# Note: After modifying `.env`, recreate the backend container (重建后端容器, e.g.
# `deploy_main.sh --recreate` or `docker compose ... up -d --no-deps backend`);
# `docker restart` does not reload `env_file`.
# Before disabling the main bucket (`STORAGE_PROVIDER=none` or empty bucket),
# operators MUST run `python -m src.dalton.storage_refs check --strict` and
# verify exit code 0 (关闭主桶前必须先跑这条命令且退出码为 0).
#
# Credentials are read strictly from environment variables and are never printed
# or passed on process command lines.

set -euo pipefail
set +x

usage() {
  cat <<'EOF'
Usage: scripts/dalton/configure_minio.sh [--role main|secondary] [--bucket NAME] [-h|--help]

Idempotently provision and verify a Dalton MinIO / S3-compatible bucket:
  - Creates bucket and resets anonymous access (`mc anonymous set none`).
  - Applies and read-back verifies bucket policy (`main`: avatars/* s3:GetObject only;
    `secondary`: strictly private).
  - Enables and verifies Bucket Versioning (`Status=Enabled`).
    IMPORTANT: Bucket Versioning cannot be disabled once enabled (only suspended).
  - Merges and verifies per-role Lifecycle rules (preserves unmanaged rule IDs;
    `uploads/` expiration is configured ONLY on `main` and removed from `secondary`).
  - Verifies MinIO `api` stale multipart upload cleanup (`MINIO_API_STALE_UPLOADS_EXPIRY=24h`).
  - Verifies CORS preflight on `secondary`, or on `main` only when
    `STORAGE_BROWSER_UPLOADS` is truthy (`1/true/yes/on`).

Options:
  --role main|secondary   Bucket role (default: main).
  --bucket NAME           Target bucket name. Optional for `main` (defaults to
                          STORAGE_BUCKET_NAME, then S3_BUCKET_NAME); REQUIRED for
                          `secondary` (e.g. --role secondary --bucket dalton-asr).
  -h, --help              Show this help and exit.

Note: After editing `.env`, recreate the backend container (重建后端容器) so
Docker Compose reloads `env_file` (`docker restart` does not).
EOF
}

ROLE="main"
BUCKET_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --role requires a value (main or secondary)." >&2
        exit 1
      fi
      ROLE="$2"
      shift 2
      ;;
    --role=*)
      ROLE="${1#*=}"
      shift
      ;;
    --bucket)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --bucket requires a bucket name." >&2
        exit 1
      fi
      BUCKET_OVERRIDE="$2"
      shift 2
      ;;
    --bucket=*)
      BUCKET_OVERRIDE="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

case "$ROLE" in
  main|secondary) ;;
  *)
    echo "ERROR: Invalid --role '$ROLE' (expected 'main' or 'secondary')." >&2
    exit 1
    ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export STORAGE_ENDPOINT_URL="${STORAGE_ENDPOINT_URL:-${S3_ENDPOINT_URL:-http://127.0.0.1:9000}}"
export STORAGE_ACCESS_KEY_ID="${STORAGE_ACCESS_KEY_ID:-${MINIO_ROOT_USER:-${AWS_ACCESS_KEY_ID:-}}}"
export STORAGE_SECRET_ACCESS_KEY="${STORAGE_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:-${AWS_SECRET_ACCESS_KEY:-}}}"
export STORAGE_REGION="${STORAGE_REGION:-${S3_REGION:-us-east-1}}"
export STORAGE_ADDRESSING_STYLE="${STORAGE_ADDRESSING_STYLE:-path}"
export STORAGE_KEY_PREFIX="${STORAGE_KEY_PREFIX:-}"
export STORAGE_BROWSER_UPLOADS="${STORAGE_BROWSER_UPLOADS:-false}"
export DALTON_CORS_ALLOWED_ORIGINS="${DALTON_CORS_ALLOWED_ORIGINS:-${MINIO_API_CORS_ALLOW_ORIGIN:-${CORS_ALLOWED_ORIGINS:-}}}"
export DALTON_UPLOADS_EXPIRE_DAYS="${DALTON_UPLOADS_EXPIRE_DAYS:-2}"
export MINIO_API_STALE_UPLOADS_EXPIRY="${MINIO_API_STALE_UPLOADS_EXPIRY:-24h}"
export MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL="${MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL:-6h}"

if [[ -n "$BUCKET_OVERRIDE" ]]; then
  TARGET_BUCKET="$BUCKET_OVERRIDE"
elif [[ "$ROLE" == "main" ]]; then
  TARGET_BUCKET="${STORAGE_BUCKET_NAME:-${S3_BUCKET_NAME:-}}"
else
  echo "ERROR: --bucket NAME is required when --role secondary (e.g. --role secondary --bucket dalton-asr)." >&2
  exit 1
fi

if [[ -z "$TARGET_BUCKET" ]]; then
  echo "ERROR: Target bucket is required (pass --bucket NAME or set STORAGE_BUCKET_NAME)." >&2
  exit 1
fi
export TARGET_BUCKET
export TARGET_ROLE="$ROLE"

if [[ -z "$STORAGE_ACCESS_KEY_ID" || -z "$STORAGE_SECRET_ACCESS_KEY" ]]; then
  echo "ERROR: STORAGE_ACCESS_KEY_ID and STORAGE_SECRET_ACCESS_KEY are required." >&2
  exit 1
fi

BROWSER_UPLOADS_NORM="$(printf '%s' "$STORAGE_BROWSER_UPLOADS" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
REQUIRE_CORS=0
if [[ "$ROLE" == "secondary" ]]; then
  REQUIRE_CORS=1
elif [[ "$BROWSER_UPLOADS_NORM" == "1" || "$BROWSER_UPLOADS_NORM" == "true" || "$BROWSER_UPLOADS_NORM" == "yes" || "$BROWSER_UPLOADS_NORM" == "on" ]]; then
  REQUIRE_CORS=1
fi

if [[ "$REQUIRE_CORS" -eq 1 && -z "$DALTON_CORS_ALLOWED_ORIGINS" ]]; then
  echo "ERROR: DALTON_CORS_ALLOWED_ORIGINS is required for role='$ROLE' with browser uploads enabled (comma-separated browser origins, e.g. https://dalton.example.com)." >&2
  exit 1
fi

MC_BIN="${MC_BIN:-$(command -v mc || true)}"
if [[ -z "$MC_BIN" || ! -x "$MC_BIN" ]]; then
  echo "ERROR: mc (MinIO Client) is required to configure and verify MinIO security and CORS, but was not found." >&2
  exit 1
fi

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

MC_TMP_DIR="$(mktemp -d)"
chmod 700 "$MC_TMP_DIR"
cleanup() {
  unset MC_HOST_dalton || true
  rm -rf "$MC_TMP_DIR"
}
trap cleanup EXIT

MC_URL="$("$PYTHON_BIN" - <<'PY'
import os
import urllib.parse

endpoint = os.environ["STORAGE_ENDPOINT_URL"].strip()
parsed = urllib.parse.urlparse(endpoint)
user = urllib.parse.quote(os.environ["STORAGE_ACCESS_KEY_ID"], safe="")
password = urllib.parse.quote(os.environ["STORAGE_SECRET_ACCESS_KEY"], safe="")
netloc = f"{user}:{password}@{parsed.netloc}"
print(urllib.parse.urlunparse((parsed.scheme, netloc, parsed.path, "", "", "")))
PY
)"
export MC_HOST_dalton="$MC_URL"
unset MC_URL

# 1 & 2. Ensure bucket exists and clear anonymous access via `mc`, then verify read-back
"$MC_BIN" --config-dir "$MC_TMP_DIR" mb --ignore-existing "dalton/${TARGET_BUCKET}" >/dev/null
"$MC_BIN" --config-dir "$MC_TMP_DIR" anonymous set none "dalton/${TARGET_BUCKET}" >/dev/null
ANON_JSON="$("$MC_BIN" --config-dir "$MC_TMP_DIR" --json anonymous get "dalton/${TARGET_BUCKET}")"
export ANON_JSON
"$PYTHON_BIN" - <<'PY'
import json
import os
import sys

data = json.loads(os.environ["ANON_JSON"])
permission = str(data.get("permission") or "").lower()
if permission not in ("none", "private"):
    print(f"ERROR: Bucket anonymous permission is {permission!r}, expected 'none'.", file=sys.stderr)
    sys.exit(1)
print(f"[minio-config] Verified mc anonymous permission reset to '{permission}'.")
PY
unset ANON_JSON

# 3. Verify MinIO `api` subsystem stale_uploads cleanup settings (without restarting MinIO)
API_CFG_OUT="$("$MC_BIN" --config-dir "$MC_TMP_DIR" admin config get dalton api)"
export API_CFG_OUT
"$PYTHON_BIN" - <<'PY'
import os
import sys

api_cfg = os.environ.get("API_CFG_OUT", "")
if "stale_uploads_expiry" not in api_cfg or "stale_uploads_cleanup_interval" not in api_cfg:
    print(
        "ERROR: MinIO `admin config get dalton api` did not report stale_uploads_expiry / stale_uploads_cleanup_interval.",
        file=sys.stderr,
    )
    sys.exit(1)
print(
    "[minio-config] Verified MinIO api stale multipart upload cleanup "
    f"(MINIO_API_STALE_UPLOADS_EXPIRY={os.environ['MINIO_API_STALE_UPLOADS_EXPIRY']}, "
    f"MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL={os.environ['MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL']})."
)
PY
unset API_CFG_OUT

# 4. S3 API policy, Bucket Versioning (Enabled), and per-role Lifecycle merge & read-back verification
PYTHONPATH="$REPO_ROOT" "$PYTHON_BIN" -m src.dalton.storage_bucket_rules apply \
  --bucket "$TARGET_BUCKET" \
  --role "$ROLE" \
  --prefix "$STORAGE_KEY_PREFIX" \
  --uploads-expire-days "$DALTON_UPLOADS_EXPIRE_DAYS"

# 5. CORS preflight verification (required on secondary; on main only when STORAGE_BROWSER_UPLOADS is truthy)
if [[ "$REQUIRE_CORS" -eq 1 ]]; then
  "$PYTHON_BIN" - <<'PY'
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

endpoint = os.environ["STORAGE_ENDPOINT_URL"].strip().rstrip("/")
bucket = os.environ["TARGET_BUCKET"].strip()
access_key = os.environ["STORAGE_ACCESS_KEY_ID"]
secret_key = os.environ["STORAGE_SECRET_ACCESS_KEY"]
region = os.environ.get("STORAGE_REGION", "us-east-1").strip() or "us-east-1"
addressing_style = os.environ.get("STORAGE_ADDRESSING_STYLE", "path").strip().lower() or "path"

raw_origins = [
    o.strip().rstrip("/")
    for o in os.environ.get("DALTON_CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
if not raw_origins:
    print("ERROR: DALTON_CORS_ALLOWED_ORIGINS must list at least one origin.", file=sys.stderr)
    sys.exit(1)
for origin in raw_origins:
    if origin == "*" or "*" in origin:
        print(
            f"ERROR: Wildcard CORS origin {origin!r} is not allowed; specify explicit browser origins.",
            file=sys.stderr,
        )
        sys.exit(1)
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        print(
            f"ERROR: Invalid CORS origin {origin!r}; expected http(s)://host[:port].",
            file=sys.stderr,
        )
        sys.exit(1)

client = boto3.client(
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

cors_config = {
    "CORSRules": [
        {
            "AllowedOrigins": raw_origins,
            "AllowedMethods": ["PUT", "GET", "HEAD"],
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag"],
            "MaxAgeSeconds": 3600,
        }
    ]
}
s3_cors_applied = False
try:
    client.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors_config)
    read_cors = client.get_bucket_cors(Bucket=bucket)
    rules = read_cors.get("CORSRules", [])
    if not rules or set(rules[0].get("AllowedOrigins", [])) != set(raw_origins):
        print("ERROR: Bucket CORS read-back verification failed.", file=sys.stderr)
        sys.exit(1)
    s3_cors_applied = True
    print(f"[minio-config] Configured and verified S3 bucket CORS on '{bucket}'.")
except ClientError as exc:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    if code not in ("NotImplemented", "MethodNotAllowed"):
        print(f"ERROR: Failed to configure S3 bucket CORS ({code}): {exc}", file=sys.stderr)
        sys.exit(1)


def verify_cors_preflight(target_origin: str, method: str, *, expect_allowed: bool) -> None:
    req = urllib.request.Request(
        f"{endpoint}/{bucket}",
        method="OPTIONS",
        headers={
            "Origin": target_origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        if expect_allowed:
            print(
                f"ERROR: CORS preflight OPTIONS for origin {target_origin!r} failed with HTTP {e.code}. "
                "Ensure MINIO_API_CORS_ALLOW_ORIGIN (or DALTON_CORS_ALLOWED_ORIGINS) is set on the minio container.",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    allow_origin = headers.get("access-control-allow-origin", "")
    allow_methods = headers.get("access-control-allow-methods", "").upper()
    expose_headers = headers.get("access-control-expose-headers", "").lower()
    if expect_allowed:
        if allow_origin != target_origin:
            print(
                f"ERROR: CORS preflight for {target_origin!r} returned "
                f"Access-Control-Allow-Origin={allow_origin!r}. "
                "Ensure MINIO_API_CORS_ALLOW_ORIGIN is injected into the minio container at startup.",
                file=sys.stderr,
            )
            sys.exit(1)
        if method not in allow_methods:
            print(
                f"ERROR: CORS preflight for {target_origin!r} does not allow {method} "
                f"(Access-Control-Allow-Methods={allow_methods!r}).",
                file=sys.stderr,
            )
            sys.exit(1)
        if expose_headers and "etag" not in expose_headers and "*" not in expose_headers:
            print(
                f"ERROR: CORS preflight for {target_origin!r} does not expose ETag "
                f"(Access-Control-Expose-Headers={expose_headers!r}).",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        if allow_origin in (target_origin, "*"):
            print(
                f"ERROR: Untrusted origin {target_origin!r} was allowed by CORS "
                f"(Access-Control-Allow-Origin={allow_origin!r}).",
                file=sys.stderr,
            )
            sys.exit(1)


for allowed_origin in raw_origins:
    for cors_method in ("PUT", "GET", "HEAD"):
        verify_cors_preflight(allowed_origin, cors_method, expect_allowed=True)
verify_cors_preflight("https://untrusted-origin.invalid", "PUT", expect_allowed=False)
if not s3_cors_applied:
    print(
        f"[minio-config] Verified active MinIO CORS preflight for origins: {', '.join(raw_origins)}"
    )
PY
else
  echo "[minio-config] Skipping browser PUT CORS preflight check for main bucket '${TARGET_BUCKET}' (STORAGE_BROWSER_UPLOADS=${STORAGE_BROWSER_UPLOADS})."
fi

echo "[minio-config] Done (role=${ROLE}, bucket=${TARGET_BUCKET})."
