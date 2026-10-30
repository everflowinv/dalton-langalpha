"""The review-window export's version header (24h review item 22, 2026-10-28).

One ``commit:`` line could not say which version the host repo, the backend
image and the two bridge processes each ran, and a container rebuild that
lost the early window read as "no backend logs". The header now names each
of them and says where the backend logs' coverage begins. Drives the script
the same way ``test_log_redaction.py`` does: a fake ``docker`` on PATH and
fixture bridge logs under ``DALTON_LOG_DIR``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
SCRIPT = SCRIPTS_DALTON / "export_review_window.sh"

# A fake docker that answers the three inspect shapes the header reads and
# echoes one log line for `logs`. The StartedAt it reports is an argument so
# each case can place the container start before or after the window.
_FAKE_DOCKER = """#!/usr/bin/env bash
if [[ "$1 $2" == "image inspect" ]]; then
  echo "{image_created}"
  exit 0
fi
if [[ "$1" == "inspect" ]]; then
  if [[ "$*" == *"{{.State.StartedAt}}"* ]]; then
    echo "{started_at}"
    exit 0
  fi
  echo "sha256:fakeimage"
  exit 0
fi
echo "2026-10-27 23:00:02,000 [INFO] backend stdout line"
echo "2026-10-27 23:00:01,000 [ERROR] backend stderr line" >&2
"""


def _run_export(
    tmp_path: Path,
    *,
    started_at: str,
    logs: dict[str, str],
    image_created: str = "2026-10-01T08:00:00Z",
) -> str:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        _FAKE_DOCKER.format(image_created=image_created, started_at=started_at),
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    for name, text in logs.items():
        (log_dir / name).write_text(text, encoding="utf-8")

    out_file = tmp_path / "exported.txt"
    proc = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--since",
            "2026-10-27 22:50",
            "-o",
            str(out_file),
        ],
        env={
            **os.environ,
            "TZ": "America/New_York",
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "DALTON_LOG_DIR": str(log_dir),
            "DALTON_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return out_file.read_text(encoding="utf-8")


def test_header_names_each_running_version_and_the_lost_coverage(tmp_path):
    """A container started inside the window says so: the missing early
    backend logs are a rebuild loss, not an empty system. The bridges' lines
    name their versions — the newest startup line wins, and a bridge that
    never logs one reads as unknown."""
    exported = _run_export(
        tmp_path,
        started_at="2026-10-28T03:30:00.123456789Z",  # 23:30 EDT, after --since
        logs={
            # 22:50 EDT = 02:50Z on the 28th: both startup lines predate it,
            # so the body drops them — the header still reads the newest.
            "dalton-researchdata-bridge.err.log": (
                "2026-10-27T02:00:00,000 [INFO] Listening on http://127.0.0.1:8787 "
                "(server_version=old-2026.10.26, max_inflight=8, caller_budget_s=120)\n"
                "2026-10-28T03:35:00,000 [INFO] Listening on http://127.0.0.1:8787 "
                "(server_version=2026.10.28-3f2a1b4, max_inflight=8, caller_budget_s=120)\n"
            ),
            "dalton-web-search-bridge.err.log": (
                "2026-10-28T03:31:00,000 [INFO] Listening on http://127.0.0.1:8788 "
                "(socket_ready=True, key_ready=True, max_inflight=4)\n"
            ),
        },
    )

    assert "researchdata_bridge_server_version: 2026.10.28-3f2a1b4" in exported
    assert "old-2026.10.26" not in exported.split("=====")[0]
    assert "websearch_bridge_server_version: unknown" in exported
    assert "backend_image_id: sha256:fakeimage" in exported
    assert "backend_image_created: 2026-10-01T08:00:00Z" in exported
    assert "backend_container_started: 2026-10-28T03:30:00.123456789Z" in exported
    assert (
        "backend_log_coverage: backend logs only cover after "
        "2026-10-28T03:30:00.123456789Z" in exported
    )
    assert "lost with the container rebuild" in exported


def test_header_reports_full_window_when_container_predates_since(tmp_path):
    exported = _run_export(
        tmp_path,
        started_at="2026-10-27T02:30:00Z",  # 22:30 EDT on the 26th, before --since
        logs={
            "dalton-researchdata-bridge.err.log": "",
        },
    )

    assert "backend_log_coverage: full window" in exported
    assert "lost with the container rebuild" not in exported
    # No startup line anywhere: the version reads as unknown, never as a guess.
    assert "researchdata_bridge_server_version: unknown" in exported


def test_the_bridge_version_is_the_newest_startup_line_across_both_logs(tmp_path):
    """The newest restart's record may sit in either file, so file order
    decides nothing: the stderr log's newer startup line beats an older one
    kept in the stdout log, and an untimed line never competes — not even
    with a version string that would sort highest."""
    exported = _run_export(
        tmp_path,
        started_at="2026-10-27T02:30:00Z",
        logs={
            "dalton-researchdata-bridge.err.log": (
                "2026-10-28T03:35:00,000 [INFO] Listening on http://127.0.0.1:8787 "
                "(server_version=2026.10.28-3f2a1b4, max_inflight=8)\n"
            ),
            "dalton-researchdata-bridge.log": (
                "Listening on http://127.0.0.1:8787 (server_version=9999-untimed)\n"
                "2026-10-27 02:00:00,000 [INFO] Listening on http://127.0.0.1:8787 "
                "(server_version=old-2026.10.26, max_inflight=8)\n"
            ),
        },
    )

    assert "researchdata_bridge_server_version: 2026.10.28-3f2a1b4" in exported
    assert "old-2026.10.26" not in exported
    assert "9999-untimed" not in exported


def test_a_version_value_lifted_from_a_log_line_is_still_redacted(tmp_path):
    exported = _run_export(
        tmp_path,
        started_at="2026-10-27T02:30:00Z",
        logs={
            "dalton-researchdata-bridge.err.log": (
                "2026-10-28T03:35:00,000 [INFO] Listening on http://127.0.0.1:8787 "
                "(server_version=1.2.3-token=sekrit_123456, max_inflight=8)\n"
            ),
        },
    )

    assert "sekrit_123456" not in exported
    assert "researchdata_bridge_server_version: 1.2.3-token=[REDACTED]" in exported
