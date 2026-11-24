"""Unit tests for scripts/standalone/upgrade.sh (Round 5 Decision #8).

Uses fake ``docker`` and ``curl`` binaries on PATH (same pattern as
``test_deploy_main_revert.py``) so tests run hermetically without a Docker daemon.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_UPGRADE_SCRIPT = _REPO_ROOT / "scripts/standalone/upgrade.sh"


def _make_fake_env(
    tmp_path: Path,
    *,
    compose_services: str = "postgres\nredis\nbackend\ncrawler\nedge\nchannel-gateway\n",
    pre_up: str = "148",
    pre_da: str = "d046",
    post_up: str = "148",
    post_da: str = "d049",
    migrate_rc: int = 0,
    new_upstream_head: str = "148",
    drain_counts: tuple[str, ...] = ("2", "0"),
) -> tuple[Path, Path, Path, Path]:
    """Create work_dir, release_dir, fake_bin, and docker trace log."""
    work_dir = tmp_path / "work"
    release_dir = tmp_path / "release"
    fake_bin = tmp_path / "bin"
    work_dir.mkdir()
    release_dir.mkdir()
    fake_bin.mkdir()

    # Minimal .env for loopback mode with Compose-managed postgres
    (work_dir / ".env").write_text(
        "\n".join(
            [
                "DB_HOST=postgres",
                "DB_USER=postgres",
                "DB_NAME=postgres",
                "DOMAIN=localhost",
                "DALTON_TLS_MODE=loopback",
                "DALTON_EDGE_PORT=8088",
                "DALTON_BACKEND_IMAGE=dalton-core-backend:local",
                "DALTON_EDGE_IMAGE=dalton-core-edge:local",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Old release files in work_dir
    for d in (work_dir, release_dir):
        (d / "scripts/standalone").mkdir(parents=True, exist_ok=True)
        (d / "deploy/standalone").mkdir(parents=True, exist_ok=True)
        (d / "migrations/versions").mkdir(parents=True, exist_ok=True)
        (d / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (d / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
        (d / "scripts/standalone/migrate.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        (d / "scripts/standalone/restore.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        (d / "deploy/standalone/Caddyfile").write_text(":80 {}\n", encoding="utf-8")

        backup_sh = d / "scripts/standalone/backup.sh"
        backup_sh.write_text(
            """#!/bin/bash
set -euo pipefail
out_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) out_dir="$2"; shift 2 ;;
    *) shift ;;
  esac
done
bdir="${out_dir}/backup-pre"
mkdir -p "$bdir"
printf 'Backup directory : %s\\n' "$bdir"
""",
            encoding="utf-8",
        )
        backup_sh.chmod(0o755)

    (work_dir / "release_marker.txt").write_text("old-v1\n", encoding="utf-8")
    (release_dir / "release_marker.txt").write_text("new-v2\n", encoding="utf-8")
    (release_dir / "new_only_file.txt").write_text("added-in-v2\n", encoding="utf-8")

    # Upstream Alembic migration files in release_dir
    (release_dir / "migrations/versions/148_base.py").write_text(
        'revision = "148"\ndown_revision = None\n',
        encoding="utf-8",
    )
    if new_upstream_head != "148":
        (release_dir / f"migrations/versions/{new_upstream_head}_next.py").write_text(
            f'revision = "{new_upstream_head}"\ndown_revision = "148"\n',
            encoding="utf-8",
        )

    trace = tmp_path / "docker.trace"
    migrated_flag = tmp_path / "migrate_ran"
    drain_idx = tmp_path / "drain_idx"
    drain_idx.write_text("0", encoding="utf-8")
    drain_lines = "\n".join(drain_counts) + "\n"
    drain_file = tmp_path / "drain_counts"
    drain_file.write_text(drain_lines, encoding="utf-8")

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "docker $*" >> "{trace}"

# Handle `docker tag ...`
if [[ "${{1:-}}" == "tag" ]]; then
  exit 0
fi

# Find subcommand after `docker compose ...`
if [[ "${{1:-}}" == "compose" ]]; then
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -p|-f|--env-file|--project-directory) shift 2 ;;
      *) break ;;
    esac
  done
  sub="${{1:-}}"
  shift || true
  case "$sub" in
    config)
      if [[ "${{1:-}}" == "--services" ]]; then
        printf '%b' '{compose_services.replace(chr(10), "\\n")}'
        exit 0
      fi
      exit 0
      ;;
    images)
      echo "img1234567890"
      exit 0
      ;;
    stop|build|up)
      exit 0
      ;;
    ps)
      echo "edge"
      exit 0
      ;;
    run)
      for arg in "$@"; do
        if [[ "$arg" == "migrate" ]]; then
          touch "{migrated_flag}"
          exit {migrate_rc}
        fi
      done
      exit 0
      ;;
    exec)
      joined="$*"
      if [[ "$joined" == *"pg_stat_activity"* ]]; then
        idx="$(cat "{drain_idx}")"
        next=$(( idx + 1 ))
        echo "$next" > "{drain_idx}"
        val="$(sed -n "${{next}}p" "{drain_file}" || true)"
        if [[ -z "$val" ]]; then
          echo "0"
        else
          echo "$val"
        fi
        exit 0
      fi
      if [[ "$joined" == *"alembic_version_dalton"* ]]; then
        if [[ -f "{migrated_flag}" ]]; then
          echo "{post_da}"
        else
          echo "{pre_da}"
        fi
        exit 0
      fi
      if [[ "$joined" == *"alembic_version"* ]]; then
        if [[ -f "{migrated_flag}" ]]; then
          echo "{post_up}"
        else
          echo "{pre_up}"
        fi
        exit 0
      fi
      # backend /health check
      exit 0
      ;;
  esac
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)

    py_link = fake_bin / "python3"
    if not py_link.exists():
        py_link.symlink_to(_REPO_ROOT / ".venv/bin/python")

    return work_dir, release_dir, fake_bin, trace


def test_standalone_upgrade_script_syntax_valid() -> None:
    res = subprocess.run(
        ["bash", "-n", str(_UPGRADE_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr


def test_standalone_upgrade_stops_services_and_drains_db_before_migrate(
    tmp_path: Path,
) -> None:
    """Decision #8: upgrade.sh stops channel-gateway/backend/crawler and drains DB before migrate."""
    work_dir, release_dir, fake_bin, trace = _make_fake_env(
        tmp_path,
        compose_services="postgres\nredis\nbackend\ncrawler\nedge\nchannel-gateway\n",
        pre_up="148",
        pre_da="d046",
        post_up="148",
        post_da="d049",
        migrate_rc=0,
        new_upstream_head="148",
        drain_counts=("1", "0"),
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "5",
    }
    res = subprocess.run(
        [
            "bash",
            str(_UPGRADE_SCRIPT),
            "--work-dir",
            str(work_dir),
            "--release-dir",
            str(release_dir),
            "--skip-build",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"

    lines = trace.read_text(encoding="utf-8").splitlines()
    stop_idx = next(
        (i for i, ln in enumerate(lines) if " stop channel-gateway backend crawler" in ln),
        None,
    )
    drain_idx = next(
        (i for i, ln in enumerate(lines) if "pg_stat_activity" in ln),
        None,
    )
    migrate_idx = next(
        (i for i, ln in enumerate(lines) if " run --rm migrate" in ln),
        None,
    )
    final_up_idx = next(
        (i for i, ln in enumerate(lines) if ln.endswith(" up -d")),
        None,
    )
    assert stop_idx is not None, f"Expected stop of channel-gateway/backend/crawler in:\n{lines}"
    assert drain_idx is not None, f"Expected pg_stat_activity drain check in:\n{lines}"
    assert migrate_idx is not None, f"Expected compose run --rm migrate in:\n{lines}"
    assert final_up_idx is not None, f"Expected final compose up -d in:\n{lines}"
    assert stop_idx < drain_idx < migrate_idx < final_up_idx
    assert (work_dir / "release_marker.txt").read_text(encoding="utf-8").strip() == "new-v2"


def test_standalone_upgrade_auto_recovers_on_pure_dalton_migrate_failure(
    tmp_path: Path,
) -> None:
    """Decision #8 (§6.3): When migrate fails with unchanged versions and upstream already at head,
    upgrade.sh automatically restores pre-upgrade files, image tags, and restarts stopped services.
    """
    work_dir, release_dir, fake_bin, trace = _make_fake_env(
        tmp_path,
        compose_services="postgres\nredis\nbackend\ncrawler\nedge\nchannel-gateway\n",
        pre_up="148",
        pre_da="d046",
        post_up="148",
        post_da="d046",
        migrate_rc=1,
        new_upstream_head="148",
        drain_counts=("0",),
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "3",
    }
    res = subprocess.run(
        [
            "bash",
            str(_UPGRADE_SCRIPT),
            "--work-dir",
            str(work_dir),
            "--release-dir",
            str(release_dir),
            "--skip-build",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "ALREADY restored the pre-upgrade release files" in res.stderr
    # Previous release file restored, new-only file removed
    assert (work_dir / "release_marker.txt").read_text(encoding="utf-8").strip() == "old-v1"
    assert not (work_dir / "new_only_file.txt").exists()

    lines = trace.read_text(encoding="utf-8").splitlines()
    # Re-tagged backend/edge images back to :local AND restarted stopped services
    assert any(
        ln.startswith("docker tag dalton-core-backend:pre-upgrade-")
        and ln.endswith(" dalton-core-backend:local")
        for ln in lines
    ), lines
    assert any(
        ln.startswith("docker tag dalton-core-edge:pre-upgrade-")
        and ln.endswith(" dalton-core-edge:local")
        for ln in lines
    ), lines
    assert any(" up -d channel-gateway backend crawler" in ln for ln in lines), lines


def test_standalone_upgrade_preserves_unknown_guidance_when_upstream_migration_pending(
    tmp_path: Path,
) -> None:
    """Decision #8: When upstream migration was pending (pre_up != new_upstream_head) and migrate fails
    with unchanged versions, do NOT auto-restart old services; emit UNCERTAIN manual check guidance.
    """
    work_dir, release_dir, fake_bin, trace = _make_fake_env(
        tmp_path,
        compose_services="postgres\nredis\nbackend\ncrawler\nedge\n",
        pre_up="148",
        pre_da="d046",
        post_up="148",
        post_da="d046",
        migrate_rc=1,
        new_upstream_head="149",
        drain_counts=("0",),
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "3",
    }
    res = subprocess.run(
        [
            "bash",
            str(_UPGRADE_SCRIPT),
            "--work-dir",
            str(work_dir),
            "--release-dir",
            str(release_dir),
            "--skip-build",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "Database migration state is UNCERTAIN" in res.stderr
    assert "ALREADY restored" not in res.stderr
    lines = trace.read_text(encoding="utf-8").splitlines()
    assert not any(" up -d " in ln for ln in lines), lines
