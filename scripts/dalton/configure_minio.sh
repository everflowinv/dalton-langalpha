#!/usr/bin/env bash
# Idempotently provision, harden, and verify the Dalton MinIO / S3-compatible bucket:
#   1. Create the bucket if it does not exist.
#   2. Enforce private access (`mc anonymous set none`, clear bucket policy, verify private).
#   3. Verify CORS for configured browser origins only (PUT / GET / HEAD, expose ETag).
#      MinIO loads CORS at startup from `MINIO_API_CORS_ALLOW_ORIGIN` in docker-compose.dalton.yml
#      (no `mc admin service restart`, which would interrupt in-flight multipart uploads).
#   4. Configure `<STORAGE_KEY_PREFIX>uploads/` 1-2 day object expiration via S3 Lifecycle
#      (`Expiration.Days`), and verify MinIO's incomplete multipart upload cleanup
#      (`api.stale_uploads_expiry` / `MINIO_API_STALE_UPLOADS_EXPIRY`, default 24h, and
#      `api.stale_uploads_cleanup_interval` / `MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL`, default 6h).
#
# Credentials are read strictly from environment variables and are never printed
# or passed on process command lines.

set -euo pipefail
set +x

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export STORAGE_ENDPOINT_URL="${STORAGE_ENDPOINT_URL:-${S3_ENDPOINT_URL:-http://127.0.0.1:9000}}"
export STORAGE_BUCKET_NAME="${STORAGE_BUCKET_NAME:-${S3_BUCKET_NAME:-}}"
export STORAGE_ACCESS_KEY_ID="${STORAGE_ACCESS_KEY_ID:-${MINIO_ROOT_USER:-${AWS_ACCESS_KEY_ID:-}}}"
export STORAGE_SECRET_ACCESS_KEY="${STORAGE_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD:-${AWS_SECRET_ACCESS_KEY:-}}}"
export STORAGE_REGION="${STORAGE_REGION:-${S3_REGION:-us-east-1}}"
export STORAGE_ADDRESSING_STYLE="${STORAGE_ADDRESSING_STYLE:-path}"
export STORAGE_KEY_PREFIX="${STORAGE_KEY_PREFIX:-}"
export DALTON_CORS_ALLOWED_ORIGINS="${DALTON_CORS_ALLOWED_ORIGINS:-${MINIO_API_CORS_ALLOW_ORIGIN:-${CORS_ALLOWED_ORIGINS:-}}}"
export DALTON_UPLOADS_EXPIRE_DAYS="${DALTON_UPLOADS_EXPIRE_DAYS:-2}"
export MINIO_API_STALE_UPLOADS_EXPIRY="${MINIO_API_STALE_UPLOADS_EXPIRY:-24h}"
export MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL="${MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL:-6h}"

if [[ -z "$STORAGE_BUCKET_NAME" ]]; then
  echo "ERROR: STORAGE_BUCKET_NAME is required." >&2
  exit 1
fi

if [[ -z "$STORAGE_ACCESS_KEY_ID" || -z "$STORAGE_SECRET_ACCESS_KEY" ]]; then
  echo "ERROR: STORAGE_ACCESS_KEY_ID and STORAGE_SECRET_ACCESS_KEY are required." >&2
  exit 1
fi

if [[ -z "$DALTON_CORS_ALLOWED_ORIGINS" ]]; then
  echo "ERROR: DALTON_CORS_ALLOWED_ORIGINS is required (comma-separated browser origins, e.g. https://dalton.example.com)." >&2
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

# 1 & 2. Ensure bucket exists and enforce anonymous=none via `mc`, then verify read-back
"$MC_BIN" --config-dir "$MC_TMP_DIR" mb --ignore-existing "dalton/${STORAGE_BUCKET_NAME}" >/dev/null
"$MC_BIN" --config-dir "$MC_TMP_DIR" anonymous set none "dalton/${STORAGE_BUCKET_NAME}" >/dev/null
# `anonymous get-json` prints the bucket policy itself (`{}` when private);
# the permission comes from `--json anonymous get`.
ANON_JSON="$("$MC_BIN" --config-dir "$MC_TMP_DIR" --json anonymous get "dalton/${STORAGE_BUCKET_NAME}")"
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
print(f"[minio-config] Verified mc anonymous permission is '{permission}'.")
PY
unset ANON_JSON

# Verify MinIO `api` subsystem stale_uploads cleanup settings (without restarting MinIO)
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

# 3 & 4. S3 API configuration and live verification (private policy, CORS preflight, and lifecycle Expiration)
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
bucket = os.environ["STORAGE_BUCKET_NAME"].strip()
access_key = os.environ["STORAGE_ACCESS_KEY_ID"]
secret_key = os.environ["STORAGE_SECRET_ACCESS_KEY"]
region = os.environ.get("STORAGE_REGION", "us-east-1").strip() or "us-east-1"
addressing_style = os.environ.get("STORAGE_ADDRESSING_STYLE", "path").strip().lower() or "path"
raw_prefix = os.environ.get("STORAGE_KEY_PREFIX", "").strip().strip("/")
key_prefix = f"{raw_prefix}/" if raw_prefix else ""
uploads_prefix = f"{key_prefix}uploads/"

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

try:
    expire_days = int(os.environ.get("DALTON_UPLOADS_EXPIRE_DAYS", "2"))
except ValueError:
    print("ERROR: DALTON_UPLOADS_EXPIRE_DAYS must be an integer (1 or 2).", file=sys.stderr)
    sys.exit(1)

if expire_days not in (1, 2):
    print("ERROR: DALTON_UPLOADS_EXPIRE_DAYS must be 1 or 2.", file=sys.stderr)
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

client.head_bucket(Bucket=bucket)

# Clear any bucket policy so access remains strictly private
try:
    client.delete_bucket_policy(Bucket=bucket)
except ClientError as exc:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    if code not in ("NoSuchBucketPolicy", "404", "MethodNotAllowed", "NotImplemented"):
        raise

# Configure S3 bucket-level CORS if supported by the server; otherwise verify
# MinIO's server-level CORS (`MINIO_API_CORS_ALLOW_ORIGIN` injected at startup).
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
        # MinIO (rs/cors) echoes only the method asked about, so each method
        # gets its own preflight rather than one asking for all three.
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

# Configure S3 Lifecycle Expiration on `<STORAGE_KEY_PREFIX>uploads/` (1-2 days).
# Note: MinIO does not support S3 `AbortIncompleteMultipartUpload` in ILM XML;
# incomplete multipart uploads are reaped by MinIO's `api.stale_uploads_expiry`
# (default 24h) verified above.
lifecycle_rules = [
    {
        "ID": "dalton-uploads-expire",
        "Status": "Enabled",
        "Filter": {"Prefix": uploads_prefix},
        "Expiration": {"Days": expire_days},
    }
]
client.put_bucket_lifecycle_configuration(
    Bucket=bucket,
    LifecycleConfiguration={"Rules": lifecycle_rules},
)
verified = client.get_bucket_lifecycle_configuration(Bucket=bucket)
matched_rules = []
for r in verified.get("Rules", []):
    rule_prefix = (r.get("Filter") or {}).get("Prefix", r.get("Prefix", ""))
    rule_days = (r.get("Expiration") or {}).get("Days")
    if (
        r.get("ID") == "dalton-uploads-expire"
        and r.get("Status") == "Enabled"
        and rule_prefix == uploads_prefix
        and rule_days == expire_days
    ):
        matched_rules.append(r.get("ID"))

if not matched_rules:
    print(
        f"ERROR: Lifecycle verification failed: rule 'dalton-uploads-expire' "
        f"(prefix={uploads_prefix!r}, days={expire_days}) not active in read-back: {verified}",
        file=sys.stderr,
    )
    sys.exit(1)

print(
    f"[minio-config] Configured and verified lifecycle expiration rule on '{bucket}' "
    f"(prefix='{uploads_prefix}', expire={expire_days}d)."
)
PY

echo "[minio-config] Done."
