#!/usr/bin/env bash
# Generate a REAL single-drive MinIO volume backup fixture using the exact production MinIO image
# pinned in docker-compose.core.yml / docker-compose.dalton.yml.
#
# Run outside the sandbox (requires Docker daemon):
#   bash scripts/dalton/ops/prod/generate_minio_inline_fixture.sh
#
# Produces:
#   tests/fixtures/dalton/minio_single_drive_fixture.tar.gz   (sanitized single-drive /data snapshot:
#       1) small inline object <128KiB stored inside xl.meta with real MinIO bitrot header
#       2) larger object >128KiB stored in <dataDir>/part.1 with real MinIO bitrot header)
#   tests/fixtures/dalton/minio_single_drive_fixture.json     (SHA-256 manifest for test verification)

set -euo pipefail
umask 077

PROD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${PROD_DIR}/../../../.." && pwd)"
MINIO_IMAGE="${DALTON_MINIO_IMAGE:-cgr.dev/chainguard/minio@sha256:e70b2b1e74d79457f808710483bd726ff364529ca30736eaa15a6ab547d3a5a7}"
FIXTURE_SRC="${REPO_ROOT}/tests/fixtures/dalton/china_ecommerce_baseline_fixture.xlsx"
OUT_TAR="${REPO_ROOT}/tests/fixtures/dalton/minio_single_drive_fixture.tar.gz"
OUT_MANIFEST="${REPO_ROOT}/tests/fixtures/dalton/minio_single_drive_fixture.json"

if [[ ! -f "${FIXTURE_SRC}" ]]; then
    echo "ERROR: fixture workbook not found: ${FIXTURE_SRC}" >&2
    exit 1
fi

TMP_DIR="$(mktemp -d)"
CONTAINER_NAME="dalton-minio-fixture-$$"
PORT="${DALTON_MINIO_FIXTURE_PORT:-19009}"

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

DATA_DIR="${TMP_DIR}/data"
mkdir -p "${DATA_DIR}"

docker run -d \
    --name "${CONTAINER_NAME}" \
    -p "127.0.0.1:${PORT}:9000" \
    -e MINIO_ROOT_USER=fixtureadmin \
    -e MINIO_ROOT_PASSWORD=fixturesecretkey \
    -v "${DATA_DIR}:/data" \
    "${MINIO_IMAGE}" \
    server /data >/dev/null

# Wait for MinIO live healthcheck
for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:${PORT}/minio/health/live" >/dev/null 2>&1; then
        break
    fi
    sleep 0.25
done

# Upload one small inline xlsx (<128KiB) and one >128KiB part.1 xlsx using stdlib S3v4 PUT (no extra deps)
python3 - "${PORT}" "${FIXTURE_SRC}" "${TMP_DIR}/large_fixture.xlsx" "${OUT_MANIFEST}" "${MINIO_IMAGE}" <<'PY'
import datetime
import hashlib
import hmac
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

port = int(sys.argv[1])
small_path = Path(sys.argv[2])
large_path = Path(sys.argv[3])
manifest_path = Path(sys.argv[4])
minio_image = sys.argv[5]

access_key = "fixtureadmin"
secret_key = "fixturesecretkey"
region = "us-east-1"
host = f"127.0.0.1:{port}"

def sign_and_put(path: str, payload: bytes) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = f"PUT\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    k_date = _hmac(("AWS4" + secret_key).encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, "s3")
    k_signing = _hmac(k_service, "aws4_request")
    sig = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}"
    )
    req = urllib.request.Request(
        f"http://{host}{path}",
        data=payload,
        method="PUT",
        headers={
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": auth,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status in (200, 201), f"PUT {path} failed: {resp.status}"

small_bytes = small_path.read_bytes()
assert len(small_bytes) < 128 * 1024, "small fixture must be <128KiB for MinIO inline xl.meta storage"

# Create a deterministic >160KiB xlsx (uncompressed extra comment part) so MinIO writes a real part.1
with zipfile.ZipFile(small_path, "r") as zin, zipfile.ZipFile(large_path, "w") as zout:
    for info in zin.infolist():
        zout.writestr(info, zin.read(info.filename), compress_type=info.compress_type)
    pad = (b"DALTON_MINIO_PART1_FIXTURE_PAD_" * 6000)
    zout.writestr("docProps/padding.bin", pad, compress_type=zipfile.ZIP_STORED)
large_bytes = large_path.read_bytes()
assert len(large_bytes) > 160 * 1024, "large fixture must exceed 128KiB for MinIO part.1 storage"

sign_and_put("/dalton-uploads", b"")
sign_and_put("/dalton-uploads/attachments/att-inline/China_Ecommerce_Model.xlsx", small_bytes)
sign_and_put("/dalton-uploads/uploads/up-part/China_Ecommerce_Large.xlsx", large_bytes)

manifest = {
    "minio_image": minio_image,
    "inline_object": {
        "key": "dalton-uploads/attachments/att-inline/China_Ecommerce_Model.xlsx",
        "size_bytes": len(small_bytes),
        "sha256": hashlib.sha256(small_bytes).hexdigest(),
    },
    "part_object": {
        "key": "dalton-uploads/uploads/up-part/China_Ecommerce_Large.xlsx",
        "size_bytes": len(large_bytes),
        "sha256": hashlib.sha256(large_bytes).hexdigest(),
    },
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY

# Stop MinIO so on-disk xl.meta & part.1 are cleanly flushed before archiving
docker stop "${CONTAINER_NAME}" >/dev/null
tar -C "${DATA_DIR}" -czf "${OUT_TAR}" dalton-uploads
echo "Generated ${OUT_TAR} and ${OUT_MANIFEST}"
