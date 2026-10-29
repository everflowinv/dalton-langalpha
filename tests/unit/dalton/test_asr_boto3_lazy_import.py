"""Hotfix A1 regression: the bridge must start on a Python without boto3.

launchd runs ``researchdata_bridge.py`` with the system Homebrew Python
(``/opt/homebrew/bin/python3``), whose third-party set is stdlib + httpx only.
The A1 merge once made the bridge import ``boto3`` at module top level through
``asr_jobs`` / ``seed_asr_client``, crashing startup with ``ModuleNotFoundError``
and taking production down. These tests pin the contract that ``boto3`` /
``botocore`` are imported lazily — only where an S3/TOS client is actually
created — by replaying the import in a subprocess with boto3 blocked out of
``sys.modules``, which is exactly what a Python without boto3 does.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

# A None entry makes `import boto3` / `from botocore.config import Config`
# raise ImportError before any module code runs — the faithful simulation of
# "boto3 is not installed" while still running under .venv's interpreter.
_BOTO3_BLOCKED_PREAMBLE = """\
import sys
for _blocked in ("boto3", "botocore", "botocore.config"):
    sys.modules[_blocked] = None
sys.path.insert(0, {bridge_dir!r})
"""


def _run_without_boto3(body: str) -> subprocess.CompletedProcess[str]:
    code = _BOTO3_BLOCKED_PREAMBLE.format(bridge_dir=str(BRIDGE_DIR)) + body
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )


def test_bridge_imports_without_boto3() -> None:
    """With boto3 blocked, `import researchdata_bridge` succeeds and the always-on helpers still work."""
    proc = _run_without_boto3(
        """\
import researchdata_bridge
import seed_asr_client

# The bridge calls these on every subprocess spawn (~16 sites); they must not
# need boto3.
assert seed_asr_client.redact_asr_secrets("x") == "x"
assert seed_asr_client.strip_asr_credentials_from_env({}) == {}

# Nothing in the bridge's top-level import graph may have touched boto3.
assert sys.modules.get("boto3") is None
assert sys.modules.get("botocore") is None
"""
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


def test_client_creation_without_boto3_raises_clear_config_error() -> None:
    """With boto3 blocked, creating the TOS / source-object clients raises the explicit non-retryable error, not ModuleNotFoundError."""
    proc = _run_without_boto3(
        """\
import asr_jobs
import seed_asr_client

creds = seed_asr_client.SeedAsrCredentials(
    app_id="app", access_key="ak", tos_ak="tak", tos_sk="tsk", tos_bucket="bucket",
)
try:
    seed_asr_client.VolcTosClient(creds)._get_s3_client()
except seed_asr_client.SeedAsrError as exc:
    assert exc.category == "asr_boto3_missing", exc.category
    assert exc.retryable is False
    assert "boto3" in exc.message and "boto3" in str(exc)
else:
    raise AssertionError("VolcTosClient._get_s3_client did not raise")

store = asr_jobs.SourceObjectStore(
    environ={
        "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://127.0.0.1:9000",
        "DALTON_ASR_STORAGE_BUCKET_NAME": "bucket",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID": "ak",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "sk",
    },
)
try:
    store._get_s3_client()
except asr_jobs.AsrWorkerError as exc:
    assert exc.category == "asr_boto3_missing", exc.category
    assert "boto3" in exc.message and "boto3" in str(exc)
else:
    raise AssertionError("SourceObjectStore._get_s3_client did not raise")
"""
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


def test_lazy_boto3_import_builds_clients_when_installed() -> None:
    """With real boto3 present (the .venv), the lazy imports still build working cached clients."""
    import asr_jobs
    import seed_asr_client as sac

    creds = sac.SeedAsrCredentials(
        app_id="app",
        access_key="ak",
        tos_ak="tak",
        tos_sk="tsk",
        tos_bucket="bucket",
    )
    tos_client = sac.VolcTosClient(creds)
    built = tos_client._get_s3_client()
    assert built is not None
    assert tos_client._get_s3_client() is built

    store = asr_jobs.SourceObjectStore(
        environ={
            "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://127.0.0.1:9000",
            "DALTON_ASR_STORAGE_BUCKET_NAME": "bucket",
            "DALTON_ASR_STORAGE_ACCESS_KEY_ID": "ak",
            "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "sk",
        }
    )
    store_built = store._get_s3_client()
    assert store_built is not None
    assert store._get_s3_client() is store_built
