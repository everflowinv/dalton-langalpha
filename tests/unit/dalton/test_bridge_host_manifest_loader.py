"""The host bridge must read the data-task manifest without importing ``src``.

The bridge runs under the host interpreter, where the backend's dependencies
are absent; importing ``src.dalton`` there fails and every non-Bloomberg host
job was rejected as ``host_command_rejected``.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]

_PROBE = r"""
import sys
from pathlib import Path
repo = Path(sys.argv[1])
sys.modules["src"] = None  # any ``import src...`` now raises ImportError
sys.path.insert(0, str(repo / "scripts" / "dalton"))
import researchdata_bridge as rb
entry = rb._load_host_job_manifest().by_job_key()["gs-pb-weekly-rundown"]
spec = rb.resolve_host_run_command_spec(
    job_key="gs-pb-weekly-rundown",
    command_argv=list(entry.command_argv),
    cwd=entry.cwd,
    data_root=repo / "var" / "dalton" / "openclaw-data",
    ocw_root=Path("/nonexistent"),
)
assert spec["command_argv"] == list(entry.command_argv), spec
print("OK", entry.runner)
"""


def test_manifest_loads_without_src_package():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(REPO)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO / "scripts" / "dalton"),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "OK host"
