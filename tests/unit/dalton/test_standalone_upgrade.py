"""Unit tests for scripts/standalone/upgrade.sh (Round 5 Decision #8 & Round 7 Decisions #5-#6).

Uses fake ``docker`` and ``curl`` binaries on PATH (same pattern as
``test_deploy_main_revert.py``) so tests run hermetically without a Docker daemon.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    new_dalton_head: str = "d049",
    drain_counts: tuple[str, ...] = ("2", "0"),
    drain_exit_rc: int = 0,
    drain_sleep_s: float = 0.0,
    alembic_sleep_s: float = 0.0,
    up_rc: int = 0,
    health_rc: int = 0,
    docker_tag_rc: int = 0,
    running_services: str | None = None,
    crawler_image: str | None = None,
    crawler_health_rc: int = 0,
    gateway_health_rc: int = 0,
    service_token: str | None = "test-internal-svc-token",
    expected_gateway_token: str | None = "test-internal-svc-token",
) -> tuple[Path, Path, Path, Path]:
    """Create work_dir, release_dir, fake_bin, and docker trace log."""
    work_dir = tmp_path / "work"
    release_dir = tmp_path / "release"
    fake_bin = tmp_path / "bin"
    work_dir.mkdir()
    release_dir.mkdir()
    fake_bin.mkdir()

    env_lines = [
        "DB_HOST=postgres",
        "DB_USER=postgres",
        "DB_NAME=postgres",
        "DOMAIN=localhost",
        "DALTON_TLS_MODE=loopback",
        "DALTON_EDGE_PORT=8088",
        "DALTON_BACKEND_IMAGE=dalton-core-backend:local",
        "DALTON_EDGE_IMAGE=dalton-core-edge:local",
    ]
    if service_token is not None:
        env_lines.append(f"INTERNAL_SERVICE_TOKEN={service_token}")
    if crawler_image:
        env_lines.append(f"DALTON_CRAWLER_IMAGE={crawler_image}")
    (work_dir / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    # Preserved cert file to verify clean tar restore keeps deploy/standalone/certs intact
    (work_dir / "deploy/standalone/certs").mkdir(parents=True, exist_ok=True)
    (work_dir / "deploy/standalone/certs/keep.pem").write_text("KEEP-CERT\n", encoding="utf-8")

    for d in (work_dir, release_dir):
        (d / "scripts/standalone").mkdir(parents=True, exist_ok=True)
        (d / "deploy/standalone").mkdir(parents=True, exist_ok=True)
        (d / "migrations/versions").mkdir(parents=True, exist_ok=True)
        (d / "src/dalton/migrations/versions").mkdir(parents=True, exist_ok=True)
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
    (release_dir / "new_only_dir").mkdir(parents=True, exist_ok=True)
    (release_dir / "new_only_dir/nested.txt").write_text("nested-v2\n", encoding="utf-8")

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

    # Dalton Alembic migration files in release_dir
    (release_dir / "src/dalton/migrations/versions/d046_base.py").write_text(
        'revision = "d046"\ndown_revision = None\n',
        encoding="utf-8",
    )
    if new_dalton_head != "d046":
        (release_dir / f"src/dalton/migrations/versions/{new_dalton_head}_next.py").write_text(
            f'revision = "{new_dalton_head}"\ndown_revision = "d046"\n',
            encoding="utf-8",
        )

    trace = tmp_path / "docker.trace"
    migrated_flag = tmp_path / "migrate_ran"
    drain_idx = tmp_path / "drain_idx"
    drain_idx.write_text("0", encoding="utf-8")
    drain_lines = "\n".join(drain_counts) + "\n"
    drain_file = tmp_path / "drain_counts"
    drain_file.write_text(drain_lines, encoding="utf-8")

    eff_running = (
        compose_services if running_services is None else running_services
    )
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "docker $*" >> "{trace}"

# Handle `docker tag ...`
if [[ "${{1:-}}" == "tag" ]]; then
  exit {docker_tag_rc}
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
    stop|build)
      exit 0
      ;;
    up)
      exit {up_rc}
      ;;
    ps)
      printf '%b' '{eff_running.replace(chr(10), "\\n")}'
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
        if [[ "{drain_sleep_s}" != "0.0" && "{drain_sleep_s}" != "0" ]]; then
          sleep {drain_sleep_s}
        fi
        if [[ {drain_exit_rc} -ne 0 ]]; then
          exit {drain_exit_rc}
        fi
        idx="$(cat "{drain_idx}")"
        next=$(( idx + 1 ))
        echo "$next" > "{drain_idx}"
        val="$(sed -n "${{next}}p" "{drain_file}" || true)"
        echo "$val"
        exit 0
      fi
      if [[ "$joined" == *"alembic_version_dalton"* ]]; then
        if [[ "{alembic_sleep_s}" != "0.0" && "{alembic_sleep_s}" != "0" ]]; then
          sleep {alembic_sleep_s}
        fi
        if [[ -f "{migrated_flag}" ]]; then
          echo "{post_da}"
        else
          echo "{pre_da}"
        fi
        exit 0
      fi
      if [[ "$joined" == *"alembic_version"* ]]; then
        if [[ "{alembic_sleep_s}" != "0.0" && "{alembic_sleep_s}" != "0" ]]; then
          sleep {alembic_sleep_s}
        fi
        if [[ -f "{migrated_flag}" ]]; then
          echo "{post_up}"
        else
          echo "{pre_up}"
        fi
        exit 0
      fi
      if [[ "$joined" == *"/health"* ]]; then
        if [[ "$joined" == *"crawler"* ]]; then
          exit {crawler_health_rc}
        fi
        if [[ "$joined" == *"channel-gateway"* ]]; then
          expected_tok="{expected_gateway_token or ''}"
          if [[ -n "$expected_tok" && "${{DALTON_HEALTH_TOKEN:-}}" != "$expected_tok" ]]; then
            exit 1
          fi
          if [[ "$joined" != *"-e DALTON_HEALTH_TOKEN"* || "$joined" != *"X-Service-Token"* ]]; then
            exit 1
          fi
          exit {gateway_health_rc}
        fi
        exit {health_rc}
      fi
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
        new_dalton_head="d049",
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
    assert stop_idx is not None
    assert drain_idx is not None
    assert migrate_idx is not None
    assert final_up_idx is not None
    assert stop_idx < drain_idx < migrate_idx < final_up_idx
    assert (work_dir / "release_marker.txt").read_text(encoding="utf-8").strip() == "new-v2"


def test_standalone_upgrade_auto_recovers_on_pure_d049_migrate_failure(
    tmp_path: Path,
) -> None:
    """R5/R7 Decision #5 & #6: Only when pre_da=d046, new_dalton_head=d049, upstream at head,
    and versions unchanged -> automatically restore files, re-tag backend/edge/crawler, restart
    stopped services, and verify /health before reporting success.
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
        new_dalton_head="d049",
        drain_counts=("0",),
        crawler_image="dalton-core-crawler:local",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "3",
        "DALTON_RECOVERY_HEALTH_RETRIES": "2",
        "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
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
    assert "backend /health passed" in res.stderr
    assert (work_dir / "release_marker.txt").read_text(encoding="utf-8").strip() == "old-v1"
    assert not (work_dir / "new_only_file.txt").exists()

    lines = trace.read_text(encoding="utf-8").splitlines()
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
    assert any(
        ln.startswith("docker tag dalton-core-crawler:pre-upgrade-")
        and ln.endswith(" dalton-core-crawler:local")
        for ln in lines
    ), lines
    assert any(" up -d channel-gateway backend crawler" in ln for ln in lines), lines
    assert any("/health" in ln for ln in lines), lines


def test_standalone_upgrade_older_dalton_version_stays_unknown_not_auto_recovered(
    tmp_path: Path,
) -> None:
    """R7 Decision #5: Upgrading from an earlier Dalton version (e.g. d042 where d043 uses
    autocommit_block) with migrate failure and unchanged versions MUST stay `unknown` and
    MUST NOT auto-restart old services!
    """
    work_dir, release_dir, fake_bin, trace = _make_fake_env(
        tmp_path,
        compose_services="postgres\nredis\nbackend\ncrawler\nedge\n",
        pre_up="148",
        pre_da="d042",
        post_up="148",
        post_da="d042",
        migrate_rc=1,
        new_upstream_head="148",
        new_dalton_head="d049",
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


def test_standalone_upgrade_preserves_unknown_guidance_when_upstream_migration_pending(
    tmp_path: Path,
) -> None:
    """Decision #5: When upstream migration was pending (pre_up != new_upstream_head) and migrate fails
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
        new_dalton_head="d049",
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


def test_standalone_upgrade_unreadable_drain_count_aborts_before_migrate_and_recovers(
    tmp_path: Path,
) -> None:
    """R7 Decision #6a: When pg_stat_activity count is unreadable, refuse to run migrate,
    restore pre-upgrade files/images/services, and exit non-zero.
    """
    work_dir, release_dir, fake_bin, trace = _make_fake_env(
        tmp_path,
        pre_up="148",
        pre_da="d046",
        post_up="148",
        post_da="d046",
        drain_exit_rc=1,
        drain_counts=("",),
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "3",
        "DALTON_MIGRATE_DRAIN_EXEC_TIMEOUT_S": "2",
        "DALTON_RECOVERY_HEALTH_RETRIES": "2",
        "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
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
    assert res.returncode != 0
    assert "BEFORE database migrations started" in res.stderr
    assert "backend /health passed" in res.stderr
    assert (work_dir / "release_marker.txt").read_text(encoding="utf-8").strip() == "old-v1"
    lines = trace.read_text(encoding="utf-8").splitlines()
    assert not any(" run --rm migrate" in ln for ln in lines), lines
    assert any(" up -d channel-gateway backend crawler" in ln for ln in lines), lines


def test_standalone_upgrade_auto_recovery_reports_up_or_health_failure_consistently(
    tmp_path: Path,
) -> None:
    """R7 Decision #6b: If `up -d` fails OR restored backend fails /health after files are restored,
    never print UNCERTAIN; clearly report files were restored while services did not come up.
    """
    for case_name, up_rc, health_rc in (("up-failed", 1, 0), ("health-failed", 0, 1)):
        case_dir = tmp_path / case_name
        case_dir.mkdir()
        work_dir, release_dir, fake_bin, _trace = _make_fake_env(
            case_dir,
            pre_up="148",
            pre_da="d046",
            post_up="148",
            post_da="d046",
            migrate_rc=1,
            new_upstream_head="148",
            new_dalton_head="d049",
            drain_counts=("0",),
            up_rc=up_rc,
            health_rc=health_rc,
        )
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "3",
            "DALTON_RECOVERY_HEALTH_RETRIES": "2",
            "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
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
        assert "WERE restored, but" in res.stderr, f"{case_name}:\n{res.stderr}"
        assert "did NOT start cleanly or failed /health" in res.stderr
        assert "UNCERTAIN" not in res.stderr
        assert (work_dir / "release_marker.txt").read_text(encoding="utf-8").strip() == "old-v1"


def test_standalone_upgrade_tar_fallback_without_rsync_purges_new_files_and_keeps_certs(
    tmp_path: Path,
) -> None:
    """R7 Decision #6d: When `rsync` is unavailable, tar restore removes files newly added by
    the failed release while preserving `.env`, `backups`, and `deploy/standalone/certs/`.
    """
    work_dir, release_dir, fake_bin, _trace = _make_fake_env(
        tmp_path,
        pre_up="148",
        pre_da="d046",
        post_up="148",
        post_da="d046",
        migrate_rc=1,
        new_upstream_head="148",
        new_dalton_head="d049",
        drain_counts=("0",),
    )
    # Build a minimal PATH directory that contains standard tools EXCEPT rsync
    no_rsync_bin = tmp_path / "no-rsync-bin"
    no_rsync_bin.mkdir()
    for tool in (
        "bash",
        "sh",
        "env",
        "tar",
        "mkdir",
        "chmod",
        "rm",
        "cp",
        "mv",
        "cat",
        "tr",
        "sed",
        "awk",
        "grep",
        "head",
        "seq",
        "sleep",
        "date",
        "basename",
        "dirname",
        "mktemp",
        "tee",
        "touch",
    ):
        found = shutil.which(tool)
        if found:
            (no_rsync_bin / tool).symlink_to(found)
    assert not (no_rsync_bin / "rsync").exists()

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{no_rsync_bin}",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "3",
        "DALTON_RECOVERY_HEALTH_RETRIES": "2",
        "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
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
    assert (work_dir / "release_marker.txt").read_text(encoding="utf-8").strip() == "old-v1"
    assert not (work_dir / "new_only_file.txt").exists()
    assert not (work_dir / "new_only_dir").exists()
    assert (work_dir / ".env").is_file()
    assert (work_dir / "deploy/standalone/certs/keep.pem").read_text(encoding="utf-8").strip() == "KEEP-CERT"


def test_r9_restore_failure_propagates_and_does_not_mark_files_restored(
    tmp_path: Path,
) -> None:
    """R9 Decision #7: A failing rsync/tag inside restore_pre_upgrade_files_and_images
    must propagate out of `set +e`, leaving AUTO_FILES_RESTORED=0 and not claiming recovery."""
    work_dir, release_dir, fake_bin, _trace = _make_fake_env(
        tmp_path,
        drain_counts=("3", "3"),
        docker_tag_rc=1,
    )
    # Also test rsync failing before docker tag
    fake_rsync = fake_bin / "rsync"
    fake_rsync.write_text("#!/bin/bash\nexit 23\n", encoding="utf-8")
    fake_rsync.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "1",
        "DALTON_RECOVERY_HEALTH_RETRIES": "1",
        "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
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
    assert "Failed to restore pre-upgrade files or image tags" in res.stderr
    assert "ALREADY restored the pre-upgrade release files" not in res.stderr


def test_r9_recovery_health_check_catches_dead_crawler_or_gateway(
    tmp_path: Path,
) -> None:
    """R9 Decision #7: Recovery health check verifies backend /health AND that crawler +
    configured channel-gateway are actually running."""
    work_dir, release_dir, fake_bin, _trace = _make_fake_env(
        tmp_path,
        compose_services="postgres\nredis\nbackend\ncrawler\nedge\nchannel-gateway\n",
        running_services="postgres\nredis\nbackend\nedge\n",  # crawler & channel-gateway exited!
        migrate_rc=1,
        pre_up="148",
        post_up="148",
        pre_da="d046",
        post_da="d046",
        health_rc=0,
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "2",
        "DALTON_RECOVERY_HEALTH_RETRIES": "2",
        "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
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
    assert "did not pass health check" in res.stderr
    assert "crawler" in res.stderr
    assert "channel-gateway" in res.stderr
    assert "Restored previous services" in res.stderr


def test_r9_drain_wall_clock_budget_and_alembic_query_timeout(
    tmp_path: Path,
) -> None:
    """R9 Decision #7: Connection drain enforces wall-clock total budget and Alembic version
    queries enforce per-query timeout."""
    import time

    work_dir, release_dir, fake_bin, _trace = _make_fake_env(
        tmp_path,
        drain_counts=("5", "5", "5", "5", "5"),
        drain_sleep_s=1.0,
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "2",
        "DALTON_MIGRATE_DRAIN_EXEC_TIMEOUT_S": "10",
        "DALTON_RECOVERY_HEALTH_RETRIES": "1",
        "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
    }
    t0 = time.monotonic()
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
    elapsed = time.monotonic() - t0
    assert res.returncode == 1
    assert "Could not confirm database connections drained to 0" in res.stderr
    # Must stop within wall-clock budget (~2s + recovery overhead), nowhere near 5*10s
    assert elapsed < 8.0, f"Drain loop exceeded wall-clock budget: {elapsed:.2f}s"

    # Also verify read_alembic_versions times out cleanly when psql hangs
    t2_dir = tmp_path / "t2"
    t2_dir.mkdir()
    work_dir2, release_dir2, fake_bin2, _trace2 = _make_fake_env(
        t2_dir,
        drain_counts=("0",),
        alembic_sleep_s=5.0,
        migrate_rc=1,
    )
    env2 = {
        **os.environ,
        "PATH": f"{fake_bin2}:{os.environ['PATH']}",
        "DALTON_MIGRATE_DRAIN_TIMEOUT_S": "2",
        "DALTON_ALEMBIC_QUERY_TIMEOUT_S": "1",
        "DALTON_RECOVERY_HEALTH_RETRIES": "1",
        "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
    }
    t1 = time.monotonic()
    res2 = subprocess.run(
        [
            "bash",
            str(_UPGRADE_SCRIPT),
            "--work-dir",
            str(work_dir2),
            "--release-dir",
            str(release_dir2),
            "--skip-build",
        ],
        env=env2,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed2 = time.monotonic() - t1
    assert res2.returncode == 1
    assert "Could not read Alembic schema versions" in res2.stderr
    assert elapsed2 < 8.0, f"Alembic version query hung past timeout: {elapsed2:.2f}s"


def test_r10_recovery_health_check_probes_crawler_and_gateway_health_endpoints(
    tmp_path: Path,
) -> None:
    """R10 Codex #10: Recovery health check probes crawler and channel-gateway /health
    endpoints, not just container running state."""
    for name, c_rc, g_rc, expect_ok, expect_err_tag in (
        ("crawler-unhealthy", 1, 0, False, "crawler(/health)"),
        ("gateway-unhealthy", 0, 1, False, "channel-gateway(/health)"),
        ("all-healthy", 0, 0, True, ""),
    ):
        case_dir = tmp_path / name
        case_dir.mkdir()
        work_dir, release_dir, fake_bin, trace = _make_fake_env(
            case_dir,
            compose_services="postgres\nredis\nbackend\ncrawler\nchannel-gateway\nedge\n",
            running_services="postgres\nredis\nbackend\ncrawler\nchannel-gateway\nedge\n",
            drain_counts=("0",),
            migrate_rc=1,
            pre_up="148",
            post_up="148",
            pre_da="d046",
            post_da="d046",
            new_upstream_head="148",
            new_dalton_head="d049",
            crawler_health_rc=c_rc,
            gateway_health_rc=g_rc,
        )
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DALTON_RECOVERY_HEALTH_RETRIES": "1",
            "DALTON_RECOVERY_HEALTH_SLEEP_S": "0",
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
        trace_txt = trace.read_text(encoding="utf-8")
        assert "exec -T crawler curl -sf http://127.0.0.1:8100/health" in trace_txt
        assert "exec -T -e DALTON_HEALTH_TOKEN channel-gateway sh -c" in trace_txt
        assert "http://127.0.0.1:8962" in trace_txt
        assert "/health" in trace_txt and "ready" in trace_txt
        assert "/agent/settings" not in trace_txt
        if expect_ok:
            assert "ALREADY restored the pre-upgrade release files" in res.stderr
            assert "health_ok=0" not in res.stderr
        else:
            assert expect_err_tag in res.stderr
            assert "health_ok=0" in res.stderr


def test_r11_gateway_health_auth_and_normal_upgrade_probes_crawler_and_gateway(
    tmp_path: Path,
) -> None:
    """R11 (Codex #5 / Grok high):
    1) channel-gateway /health probe reads INTERNAL_SERVICE_TOKEN from .env and sends
       X-Service-Token (missing or mismatched token fails like HTTP 401, and token never leaks
       into docker args/logs);
    2) Normal upgrade branch probes crawler and configured channel-gateway /health before
       reporting Upgrade Complete;
    3) Exact sh -c probe snippet extracted from upgrade.sh executes both the curl branch and
       the node fallback branch, failing on missing/wrong token and succeeding only on match.
    """
    import re as _re

    # 1. Normal upgrade succeeds when token matches and both crawler + gateway are healthy;
    #    secret token must NOT appear in docker trace or stderr.
    secret_tok = "tok-r11-super-secret-xyz987"
    ok_dir = tmp_path / "normal-ok"
    ok_dir.mkdir()
    work_dir, release_dir, fake_bin, trace = _make_fake_env(
        ok_dir,
        compose_services="postgres\nredis\nbackend\ncrawler\nchannel-gateway\nedge\n",
        running_services="postgres\nredis\nbackend\ncrawler\nchannel-gateway\nedge\n",
        drain_counts=("0",),
        migrate_rc=0,
        service_token=secret_tok,
        expected_gateway_token=secret_tok,
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DALTON_POST_UPGRADE_HEALTH_RETRIES": "1",
        "DALTON_POST_UPGRADE_HEALTH_SLEEP_S": "0",
    }
    res_ok = subprocess.run(
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
    assert res_ok.returncode == 0, res_ok.stderr
    trace_txt = trace.read_text(encoding="utf-8")
    assert "exec -T crawler curl -sf http://127.0.0.1:8100/health" in trace_txt
    assert "exec -T -e DALTON_HEALTH_TOKEN channel-gateway sh -c" in trace_txt
    assert "X-Service-Token" in trace_txt
    assert secret_tok not in trace_txt
    assert secret_tok not in res_ok.stdout
    assert secret_tok not in res_ok.stderr

    # 2. Normal upgrade fails when channel-gateway token is missing or wrong (401), or crawler fails /health
    for case_name, svc_tok, exp_tok, c_rc, g_rc, expect_tag in (
        ("missing-token", "", secret_tok, 0, 0, "channel-gateway(/health)"),
        ("wrong-token", "wrong-tok", secret_tok, 0, 0, "channel-gateway(/health)"),
        ("normal-crawler-fail", secret_tok, secret_tok, 1, 0, "crawler(/health)"),
        ("normal-gateway-fail", secret_tok, secret_tok, 0, 1, "channel-gateway(/health)"),
    ):
        cdir = tmp_path / case_name
        cdir.mkdir()
        w_dir, r_dir, f_bin, _ = _make_fake_env(
            cdir,
            compose_services="postgres\nredis\nbackend\ncrawler\nchannel-gateway\nedge\n",
            running_services="postgres\nredis\nbackend\ncrawler\nchannel-gateway\nedge\n",
            drain_counts=("0",),
            migrate_rc=0,
            service_token=svc_tok,
            expected_gateway_token=exp_tok,
            crawler_health_rc=c_rc,
            gateway_health_rc=g_rc,
        )
        c_env = {
            **os.environ,
            "PATH": f"{f_bin}:{os.environ['PATH']}",
            "DALTON_POST_UPGRADE_HEALTH_RETRIES": "1",
            "DALTON_POST_UPGRADE_HEALTH_SLEEP_S": "0",
        }
        res_fail = subprocess.run(
            [
                "bash",
                str(_UPGRADE_SCRIPT),
                "--work-dir",
                str(w_dir),
                "--release-dir",
                str(r_dir),
                "--skip-build",
            ],
            env=c_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_fail.returncode != 0, f"{case_name}: expected failure"
        assert expect_tag in res_fail.stderr, f"{case_name}: {res_fail.stderr}"

    # 3. Execute the exact channel-gateway sh -c readiness probe from upgrade.sh in both
    #    the curl branch and the node branch without opening network sockets
    #    (unit tests run under pytest-socket):
    #    - probe MUST NOT fall back to /agent/settings;
    #    - missing/wrong token -> fail;
    #    - /health {"ok":true,"ready":false} -> fail (still starting, continues polling);
    #    - /health {"ok":true} (missing ready field) -> fail (treated as not ready until timeout);
    #    - /health {"ok":false,"ready":true} -> fail;
    #    - /health {"ok":true,"ready":true} -> pass.
    script_src = _UPGRADE_SCRIPT.read_text(encoding="utf-8")
    m_probe = _re.search(
        r"exec -T -e DALTON_HEALTH_TOKEN channel-gateway sh -c \\\n\s+'([^']+)'",
        script_src,
    )
    assert m_probe is not None, "Expected channel-gateway sh -c readiness probe in upgrade.sh"
    probe_snippet = m_probe.group(1)
    assert "/agent/settings" not in probe_snippet

    base_url = "http://127.0.0.1:8962"
    base_probe_env = {k: v for k, v in os.environ.items() if k != "DALTON_HEALTH_TOKEN"}

    # 3a. curl branch (PATH contains fake curl + real python3, NO node)
    curl_bin = tmp_path / "probe-curl-bin"
    curl_bin.mkdir()
    py3_bin = Path(sys.executable)
    (curl_bin / "python3").symlink_to(py3_bin)
    fake_curl = curl_bin / "curl"
    fake_curl.write_text(
        f"""#!/bin/bash
set -euo pipefail
hdr=""
target=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -H)
      hdr="${{2:-}}"
      shift 2
      ;;
    -sf|-s|-f)
      shift
      ;;
    *)
      target="$1"
      shift
      ;;
  esac
done
if [[ "$hdr" != "X-Service-Token: {secret_tok}" ]]; then
  exit 22
fi
if [[ "$target" == "{base_url}/health" ]]; then
  printf '%s' "${{FAKE_HEALTH_BODY:-}}"
  exit "${{FAKE_HEALTH_RC:-0}}"
fi
exit 22
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    curl_env = {**base_probe_env, "PATH": str(curl_bin)}
    for label, extra_env, expect_ok in (
        ("missing-token", {}, False),
        ("wrong-token", {"DALTON_HEALTH_TOKEN": "wrong-token", "FAKE_HEALTH_BODY": '{"ok":true,"ready":true}'}, False),
        ("health-ready-false", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok": true, "ready": false}'}, False),
        ("health-missing-ready", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok": true}'}, False),
        ("health-ok-false", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok": false, "ready": true}'}, False),
        ("health-nested-ready-true-top-false", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok":true,"ready":false,"checks":{"ready":true}}'}, False),
        ("health-invalid-json-trueish", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok":true,"ready":trueish}'}, False),
        ("health-invalid-json-nan", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok":true,"ready":true,"value":NaN}'}, False),
        ("health-invalid-json-infinity", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok":true,"ready":true,"value":Infinity}'}, False),
        ("health-invalid-json-neg-infinity", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok":true,"ready":true,"value":-Infinity}'}, False),
        ("health-ready-string-true", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok":true,"ready":"true"}'}, False),
        ("health-ready-int-one", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok":1,"ready":1}'}, False),
        ("health-ready-true", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok": true, "ready": true}'}, True),
        ("health-ready-true-reversed-keys", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ready": true, "ok": true}'}, True),
    ):
        r_c = subprocess.run(
            ["/bin/sh", "-c", probe_snippet, "_", base_url],
            env={**curl_env, **extra_env},
            text=True,
            capture_output=True,
            check=False,
        )
        assert (r_c.returncode == 0) is expect_ok, f"curl branch {label}: rc={r_c.returncode} err={r_c.stderr}"

    # 3a-ii. curl branch without python3 -> fails closed (not ready)
    curl_nopy_bin = tmp_path / "probe-curl-nopy-bin"
    curl_nopy_bin.mkdir()
    (curl_nopy_bin / "curl").symlink_to(fake_curl)
    r_nopy = subprocess.run(
        ["/bin/sh", "-c", probe_snippet, "_", base_url],
        env={
            **base_probe_env,
            "PATH": str(curl_nopy_bin),
            "DALTON_HEALTH_TOKEN": secret_tok,
            "FAKE_HEALTH_BODY": '{"ok":true,"ready":true}',
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert r_nopy.returncode != 0, "curl branch without python3 must fail closed"

    # 3b. node branch (PATH has fake node; verifies JS checks /health ok===true && ready===true only)
    node_bin = tmp_path / "probe-node-bin"
    node_bin.mkdir()
    fake_node = node_bin / "node"
    fake_node.write_text(
        f"""#!/bin/bash
set -euo pipefail
if [[ "${{1:-}}" != "-e" ]]; then exit 2; fi
js="${{2:-}}"
target="${{3:-}}"
if [[ "$js" != *"X-Service-Token"* || "$js" != *"DALTON_HEALTH_TOKEN"* ]]; then exit 3; fi
if [[ "$js" != *"/health"* || "$js" != *"ready===true"* ]]; then exit 5; fi
if [[ "$js" == *"/agent/settings"* ]]; then exit 6; fi
if [[ "$target" != "{base_url}" ]]; then exit 4; fi
if [[ "${{DALTON_HEALTH_TOKEN:-}}" != "{secret_tok}" ]]; then exit 1; fi
exit 0
""",
        encoding="utf-8",
    )
    fake_node.chmod(0o755)
    node_env = {**base_probe_env, "PATH": str(node_bin)}
    r_node_wrong = subprocess.run(
        ["/bin/sh", "-c", probe_snippet, "_", base_url],
        env={**node_env, "DALTON_HEALTH_TOKEN": "wrong-token"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert r_node_wrong.returncode != 0
    r_node_good = subprocess.run(
        ["/bin/sh", "-c", probe_snippet, "_", base_url],
        env={**node_env, "DALTON_HEALTH_TOKEN": secret_tok},
        text=True,
        capture_output=True,
        check=False,
    )
    assert r_node_good.returncode == 0, r_node_good.stderr

    # 3c. Verify upgrade.sh polling behavior: ready:false -> keeps polling and times out;
    #     ready:false on attempt 1 then ready:true on attempt 2 -> succeeds!
    poll_dir = tmp_path / "poll-ready-transition"
    poll_dir.mkdir()
    poll_count_file = poll_dir / "gw_health_calls"
    poll_count_file.write_text("0", encoding="utf-8")
    w_p, r_p, fb_p, _ = _make_fake_env(
        poll_dir,
        compose_services="postgres\nredis\nbackend\ncrawler\nchannel-gateway\nedge\n",
        running_services="postgres\nredis\nbackend\ncrawler\nchannel-gateway\nedge\n",
        drain_counts=("0",),
        migrate_rc=0,
        service_token=secret_tok,
        expected_gateway_token=secret_tok,
    )
    # Patch fake docker so attempt 1 fails (simulating ready:false) and attempt 2 succeeds (ready:true)
    fd_path = fb_p / "docker"
    fd_src = fd_path.read_text(encoding="utf-8")
    fd_src = fd_src.replace(
        "exit 0\n        fi\n        exit 0",
        f'c="$(cat "{poll_count_file}")"\nc=$((c + 1))\necho "$c" > "{poll_count_file}"\nif [[ "$c" -lt 2 ]]; then exit 1; fi\nexit 0\n        fi\n        exit 0',
        1,
    )
    fd_path.write_text(fd_src, encoding="utf-8")
    res_poll = subprocess.run(
        [
            "bash",
            str(_UPGRADE_SCRIPT),
            "--work-dir",
            str(w_p),
            "--release-dir",
            str(r_p),
            "--skip-build",
        ],
        env={
            **os.environ,
            "PATH": f"{fb_p}:{os.environ['PATH']}",
            "DALTON_POST_UPGRADE_HEALTH_RETRIES": "3",
            "DALTON_POST_UPGRADE_HEALTH_SLEEP_S": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_poll.returncode == 0, res_poll.stderr
    assert int(poll_count_file.read_text(encoding="utf-8").strip()) == 2


def test_upgrade_backfills_missing_research_host_tokens_and_verifies_wiki_mcp(tmp_path: Path):
    """Item #2: upgrade.sh backfills missing research-host tokens without overwriting existing ones,
    and runs an authenticated /mcp/company-wiki tools/list probe."""
    from dotenv import dotenv_values
    from fastapi.testclient import TestClient

    from src.dalton.profile import DeploymentProfile
    from src.dalton.research_host.app import ResearchHostConfig, create_app

    # Part A: old .env missing DALTON_RESEARCH_HOST_TOKEN and DALTON_RESEARCH_ADMIN_TOKEN
    missing_dir = tmp_path / "missing_tokens"
    missing_dir.mkdir()
    w_a, r_a, fb_a, _tr_a = _make_fake_env(
        missing_dir,
        compose_services="postgres\nredis\nbackend\ncrawler\nresearch-host\nedge\n",
        pre_up="148",
        pre_da="d049",
        post_up="148",
        post_da="d049",
        new_upstream_head="148",
        new_dalton_head="d049",
    )
    probe_token_log = tmp_path / "rh_probe_token.txt"
    fd_path = fb_a / "docker"
    fd_src = fd_path.read_text(encoding="utf-8")
    rh_probe_handler = f"""      if [[ "$joined" == *"research-host"* && "$joined" == *"/healthz"* ]]; then
        echo "enabled"
        exit 0
      fi
      if [[ "$joined" == *"research-host"* && "$joined" == *"/mcp/company-wiki"* ]]; then
        printf '%s' "${{DALTON_RH_PROBE_TOKEN:-}}" > "{probe_token_log}"
        if [[ -z "${{DALTON_RH_PROBE_TOKEN:-}}" ]]; then
          exit 1
        fi
        exit 0
      fi
      if [[ "$joined" == *"/health"* ]]; then"""
    assert '      if [[ "$joined" == *"/health"* ]]; then' in fd_src
    fd_path.write_text(
        fd_src.replace('      if [[ "$joined" == *"/health"* ]]; then', rh_probe_handler, 1),
        encoding="utf-8",
    )

    res_a = subprocess.run(
        [
            "bash",
            str(_UPGRADE_SCRIPT),
            "--work-dir",
            str(w_a),
            "--release-dir",
            str(r_a),
            "--skip-build",
        ],
        env={
            **os.environ,
            "PATH": f"{fb_a}:{os.environ['PATH']}",
            "DALTON_POST_UPGRADE_HEALTH_RETRIES": "1",
            "DALTON_POST_UPGRADE_HEALTH_SLEEP_S": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_a.returncode == 0, f"stdout:\n{res_a.stdout}\nstderr:\n{res_a.stderr}"

    env_vals = dotenv_values(w_a / ".env")
    host_tok = env_vals.get("DALTON_RESEARCH_HOST_TOKEN") or ""
    admin_tok = env_vals.get("DALTON_RESEARCH_ADMIN_TOKEN") or ""
    assert len(host_tok) == 64 and all(c in "0123456789abcdef" for c in host_tok)
    assert len(admin_tok) == 64 and all(c in "0123456789abcdef" for c in admin_tok)
    assert host_tok != admin_tok
    assert env_vals.get("DB_HOST") == "postgres"
    assert env_vals.get("DOMAIN") == "localhost"
    assert (w_a / ".env").stat().st_mode & 0o777 == 0o600
    assert probe_token_log.read_text(encoding="utf-8") == host_tok

    # Verify the backfilled token actually unlocks authenticated company-wiki tools/list
    wiki_dir = tmp_path / "upgrade_wiki_data"
    cfg = ResearchHostConfig(
        profile=DeploymentProfile.CORE,
        data_root=wiki_dir,
        mcp_token=host_tok,
        admin_token=admin_tok,
    )
    app = create_app(cfg)
    client = TestClient(app)
    unauth = client.post(
        "/mcp/company-wiki",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert unauth.status_code == 401
    authed = client.post(
        "/mcp/company-wiki",
        headers={"Authorization": f"Bearer {host_tok}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert authed.status_code == 200, authed.text
    tool_names = [t["name"] for t in authed.json()["result"]["tools"]]
    assert "search" in tool_names

    # Part B: existing tokens in .env must NEVER be overwritten
    existing_dir = tmp_path / "existing_tokens"
    existing_dir.mkdir()
    w_b, r_b, fb_b, _tr_b = _make_fake_env(
        existing_dir,
        compose_services="postgres\nredis\nbackend\ncrawler\nresearch-host\nedge\n",
        pre_up="148",
        pre_da="d049",
        post_up="148",
        post_da="d049",
        new_upstream_head="148",
        new_dalton_head="d049",
    )
    with (w_b / ".env").open("a", encoding="utf-8") as fh:
        fh.write("DALTON_RESEARCH_HOST_TOKEN=existing-host-token-keep-me\n")
        fh.write("DALTON_RESEARCH_ADMIN_TOKEN=existing-admin-token-keep-me\n")
    fd_b = fb_b / "docker"
    fd_b_src = fd_b.read_text(encoding="utf-8")
    fd_b.write_text(
        fd_b_src.replace(
            '      if [[ "$joined" == *"/health"* ]]; then',
            """      if [[ "$joined" == *"research-host"* && "$joined" == *"/healthz"* ]]; then
        echo "enabled"
        exit 0
      fi
      if [[ "$joined" == *"research-host"* && "$joined" == *"/mcp/company-wiki"* ]]; then
        if [[ "${DALTON_RH_PROBE_TOKEN:-}" != "existing-host-token-keep-me" ]]; then
          exit 1
        fi
        exit 0
      fi
      if [[ "$joined" == *"/health"* ]]; then""",
            1,
        ),
        encoding="utf-8",
    )
    res_b = subprocess.run(
        [
            "bash",
            str(_UPGRADE_SCRIPT),
            "--work-dir",
            str(w_b),
            "--release-dir",
            str(r_b),
            "--skip-build",
        ],
        env={
            **os.environ,
            "PATH": f"{fb_b}:{os.environ['PATH']}",
            "DALTON_POST_UPGRADE_HEALTH_RETRIES": "1",
            "DALTON_POST_UPGRADE_HEALTH_SLEEP_S": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_b.returncode == 0, f"stdout:\n{res_b.stdout}\nstderr:\n{res_b.stderr}"
    env_vals_b = dotenv_values(w_b / ".env")
    assert env_vals_b.get("DALTON_RESEARCH_HOST_TOKEN") == "existing-host-token-keep-me"
    assert env_vals_b.get("DALTON_RESEARCH_ADMIN_TOKEN") == "existing-admin-token-keep-me"

    # Part C (r7 #2): DALTON_CAP_COMPANY_WIKI=' false ' disables company-wiki in research-host;
    # upgrade.sh reads actual capability state from running `/healthz` (not bash .env parsing)
    # and skips the /mcp/company-wiki probe without failing.
    from src.dalton.capabilities import CapabilityId, effective_capabilities
    from src.dalton.config_env import load_shared_dotenv

    disabled_dir = tmp_path / "wiki_disabled_quoted"
    disabled_dir.mkdir()
    w_c, r_c, fb_c, _tr_c = _make_fake_env(
        disabled_dir,
        compose_services="postgres\nredis\nbackend\ncrawler\nresearch-host\nedge\n",
        pre_up="148",
        pre_da="d049",
        post_up="148",
        post_da="d049",
        new_upstream_head="148",
        new_dalton_head="d049",
    )
    with (w_c / ".env").open("a", encoding="utf-8") as fh:
        fh.write("APP_ENV=core\n")
        fh.write("DALTON_CAP_COMPANY_WIKI=' false '\n")

    # Confirm real Python shared loader + research-host /healthz report company-wiki=False
    iso_env: dict[str, str] = {}
    load_shared_dotenv(w_c / ".env", environ=iso_env)
    iso_caps = effective_capabilities(profile=DeploymentProfile.CORE, environ=iso_env)
    assert CapabilityId.COMPANY_WIKI not in iso_caps
    app_c = create_app(
        ResearchHostConfig(
            profile=DeploymentProfile.CORE,
            data_root=tmp_path / "disabled_wiki_data",
            mcp_token="tok-c-mcp",
            admin_token="tok-c-adm",
        ),
        capabilities_provider=lambda: iso_caps,
    )
    hz_c = TestClient(app_c).get("/healthz").json()
    assert hz_c["ok"] is True and hz_c["connectors"]["company-wiki"] is False
    expected_state = "enabled" if hz_c["connectors"]["company-wiki"] else "disabled"

    mcp_probe_called = tmp_path / "wiki_disabled_mcp_called"
    fd_c = fb_c / "docker"
    fd_c_src = fd_c.read_text(encoding="utf-8")
    fd_c.write_text(
        fd_c_src.replace(
            '      if [[ "$joined" == *"/health"* ]]; then',
            f"""      if [[ "$joined" == *"research-host"* && "$joined" == *"/healthz"* ]]; then
        echo "{expected_state}"
        exit 0
      fi
      if [[ "$joined" == *"research-host"* && "$joined" == *"/mcp/company-wiki"* ]]; then
        touch "{mcp_probe_called}"
        exit 1
      fi
      if [[ "$joined" == *"/health"* ]]; then""",
            1,
        ),
        encoding="utf-8",
    )
    res_c = subprocess.run(
        [
            "bash",
            str(_UPGRADE_SCRIPT),
            "--work-dir",
            str(w_c),
            "--release-dir",
            str(r_c),
            "--skip-build",
        ],
        env={
            **os.environ,
            "PATH": f"{fb_c}:{os.environ['PATH']}",
            "DALTON_POST_UPGRADE_HEALTH_RETRIES": "1",
            "DALTON_POST_UPGRADE_HEALTH_SLEEP_S": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_c.returncode == 0, f"stdout:\n{res_c.stdout}\nstderr:\n{res_c.stderr}"
    assert not mcp_probe_called.exists(), "upgrade.sh must not probe /mcp/company-wiki when /healthz reports it disabled"


def test_backup_and_restore_include_wiki_volume_sqlite_originals_and_receipts(tmp_path: Path):
    """Item #3: backup.sh & restore.sh round-trip the Wiki data volume
    (SQLite online backup, Markdown, originals binary, image index, audit receipts)."""
    import asyncio
    import base64
    import hashlib
    import io
    import json
    import sqlite3
    import tarfile

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from src.dalton.research_host.wiki_store import OpenWikiStore

    backup_script = _REPO_ROOT / "scripts/standalone/backup.sh"
    restore_script = _REPO_ROOT / "scripts/standalone/restore.sh"

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (work_dir / "docker-compose.core.yml").write_text("services: {}\n", encoding="utf-8")
    (work_dir / ".env").write_text(
        "\n".join(
            [
                "COMPOSE_PROJECT_NAME=dalton_wiki_br_test",
                "DB_HOST=postgres",
                "DB_USER=postgres",
                "DB_PASSWORD=postgres",
                "DB_NAME=postgres",
                "DOMAIN=localhost",
                "DALTON_TLS_MODE=loopback",
                "DALTON_EDGE_PORT=8088",
                "DALTON_CHAT_UPLOAD_STORAGE_PROVIDER=s3",
                "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT=http://minio:9000",
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=chat-uploads",
                "MINIO_ROOT_USER=minioadmin",
                "MINIO_ROOT_PASSWORD=minioadmin",
                "DALTON_RESEARCH_ADMIN_TOKEN=backup-test-admin-token-1234",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Seed source Wiki volume with a compressed PDF + PNG image via OpenWikiStore
    src_wiki = tmp_path / "src_wiki"
    dst_wiki = tmp_path / "dst_wiki"
    dst_wiki.mkdir()
    store = OpenWikiStore(src_wiki)

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font_dict = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font_dict)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 72 720 Td (BackupRestoreUniqueCompressedPdfMarker 2026Q4) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    page.compress_content_streams()
    pdf_buf = io.BytesIO()
    writer.write(pdf_buf)
    pdf_bytes = pdf_buf.getvalue()

    sc_pdf, pdf_receipt = asyncio.run(
        store.ingest(
            {
                "request_id": "br-req-pdf-001",
                "target_type": "company",
                "target": "BRCorp",
                "content_type": "研报笔记",
                "title": "BR Compressed Memo",
                "date": "2026-11-26",
                "source": "backup-test",
                "tags": ["backup-verify"],
                "file": {
                    "filename": "memo.pdf",
                    "content_b64": base64.b64encode(pdf_bytes).decode("ascii"),
                },
            }
        )
    )
    assert sc_pdf == 200 and pdf_receipt["ok"] is True, pdf_receipt

    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )
    sc_img, img_receipt = asyncio.run(
        store.ingest(
            {
                "request_id": "br-req-img-001",
                "target_type": "company",
                "target": "BRCorp",
                "content_type": "研报笔记",
                "title": "BR Chart Image",
                "date": "2026-11-26",
                "source": "backup-test",
                "body": "BackupRestoreImageSummaryText",
                "file": {
                    "filename": "chart.png",
                    "content_b64": base64.b64encode(png_bytes).decode("ascii"),
                },
            }
        )
    )
    assert sc_img == 200 and img_receipt["ok"] is True, img_receipt

    # Hermetic fake docker/curl that execute backup.sh & restore.sh's REAL wiki Python/tar commands
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    py_exe = sys.executable
    pause_events_log = tmp_path / "wiki_pause_events.log"
    lease_state_file = tmp_path / "wiki_lease_state.json"
    fail_wiki_backup_flag = tmp_path / "fail_wiki_backup.flag"
    fail_wiki_resume_flag = tmp_path / "fail_wiki_resume.flag"
    hold_backup_gate_flag = tmp_path / "hold_backup_gate.flag"
    in_backup_marker = tmp_path / "in_backup_marker.flag"
    backup_sleep_file = tmp_path / "backup_sleep_s.txt"

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env python3
import fcntl, io, json, os, subprocess, sys, tarfile, time

args = sys.argv[1:]
if not args or args[0] != "compose":
    sys.exit(0)

# Strip compose global flags
i = 1
while i < len(args):
    if args[i] in ("-p", "-f", "--env-file", "--project-directory"):
        i += 2
    else:
        break
sub = args[i] if i < len(args) else ""
rest = args[i + 1 :]

if sub == "config":
    if "--services" in rest:
        sys.stdout.write("postgres\\nredis\\nminio\\nbackend\\nedge\\nresearch-host\\n")
        sys.exit(0)
    if "--format" in rest and "json" in rest:
        cfg = {{
            "services": {{
                "backend": {{
                    "environment": {{
                        "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL": "http://minio:9000",
                        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "chat-uploads",
                        "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID": "minioadmin",
                        "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY": "minioadmin",
                        "MINIO_ROOT_USER": "minioadmin",
                        "MINIO_ROOT_PASSWORD": "minioadmin",
                    }}
                }}
            }}
        }}
        sys.stdout.write(json.dumps(cfg))
        sys.exit(0)
    sys.exit(0)

if sub == "ps":
    sys.stdout.write("postgres\\nredis\\nminio\\nbackend\\nedge\\nresearch-host\\n")
    sys.exit(0)

if sub in ("up", "build", "stop"):
    sys.exit(0)

if sub == "port":
    sys.stdout.write("127.0.0.1:8088\\n")
    sys.exit(0)

if sub == "exec":
    joined = " ".join(rest)
    if "research-host" in rest:
        if "/admin/wiki/pause-writes" in joined:
            tok = os.environ.get("DALTON_RH_ADMIN_TOKEN", "")
            if not tok:
                sys.exit(1)
            owner = os.environ.get("DALTON_RH_PAUSE_OWNER", "")
            lease_id_env = os.environ.get("DALTON_RH_PAUSE_LEASE_ID", "").strip()
            ttl_env = os.environ.get("DALTON_RH_PAUSE_TTL_S", "").strip()
            state_path = {str(lease_state_file)!r}
            lock_fd = os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if os.path.exists(state_path):
                    with open(state_path, encoding="utf-8") as sf:
                        st = json.load(sf)
                else:
                    st = {{"next_id": 0, "paused": False, "owner": None, "lease_id": 0, "expires_at": 0.0}}
                now = time.time()
                if st["paused"] and now >= float(st.get("expires_at") or 0.0):
                    st["paused"] = False
                    st["owner"] = None
                    st["lease_id"] = 0
                with open({str(pause_events_log)!r}, "a", encoding="utf-8") as lf:
                    if "DALTON_RH_PAUSE_TTL_S" in joined and "DALTON_RH_PAUSE_LEASE_ID" in joined:
                        # Heartbeat renewal
                        req_lid = int(lease_id_env)
                        ttl_s = float(ttl_env or "300")
                        if not st["paused"] or st["lease_id"] != req_lid or st["owner"] != owner:
                            lf.write(f"renew_expired:{{owner}}:{{req_lid}}\\n")
                            with open(state_path, "w", encoding="utf-8") as sf:
                                json.dump(st, sf)
                            sys.exit(1)
                        st["expires_at"] = now + ttl_s
                        lf.write(f"renew:{{owner}}:{{req_lid}}\\n")
                        with open(state_path, "w", encoding="utf-8") as sf:
                            json.dump(st, sf)
                        sys.exit(0)
                    elif "DALTON_RH_PAUSE_TTL_S" in joined:
                        # Initial pause acquire
                        if not owner:
                            sys.exit(1)
                        ttl_s = float(ttl_env or "300")
                        if st["paused"] and st["owner"] != owner:
                            sys.exit(1)
                        st["next_id"] += 1
                        st["paused"] = True
                        st["owner"] = owner
                        st["lease_id"] = st["next_id"]
                        st["expires_at"] = now + ttl_s
                        lf.write(f"pause:{{owner}}:{{st['lease_id']}}\\n")
                        with open(state_path, "w", encoding="utf-8") as sf:
                            json.dump(st, sf)
                        sys.stdout.write(f"{{st['lease_id']}}\\n")
                        sys.exit(0)
                    elif "DALTON_RH_PAUSE_OWNER" in joined:
                        # Resume
                        if not owner:
                            sys.exit(1)
                        if os.path.exists({str(fail_wiki_resume_flag)!r}):
                            lf.write(f"resume_fail:{{owner}}\\n")
                            sys.exit(1)
                        req_lid = int(lease_id_env) if lease_id_env else 0
                        if not st["paused"]:
                            lf.write(f"resume_expired:{{owner}}:{{req_lid}}\\n")
                            with open(state_path, "w", encoding="utf-8") as sf:
                                json.dump(st, sf)
                            sys.stdout.write("EXPIRED\\n")
                            sys.exit(0)
                        if st["owner"] != owner or (req_lid and st["lease_id"] != req_lid):
                            sys.exit(1)
                        st["paused"] = False
                        st["owner"] = None
                        st["lease_id"] = 0
                        st["expires_at"] = 0.0
                        lf.write(f"resume:{{owner}}:{{req_lid}}\\n")
                        with open(state_path, "w", encoding="utf-8") as sf:
                            json.dump(st, sf)
                        sys.stdout.write("OK\\n")
                        sys.exit(0)
                    else:
                        lf.write("poll_in_flight\\n")
                        sys.stdout.write("0\\n")
                        sys.exit(0)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        idx = rest.index("-c")
        script = rest[idx + 1]
        with open({str(pause_events_log)!r}, "a", encoding="utf-8") as lf:
            lf.write("backup_python\\n")
        if os.path.exists({str(backup_sleep_file)!r}):
            with open({str(backup_sleep_file)!r}, encoding="utf-8") as bsf:
                sleep_dur = float(bsf.read().strip() or "0")
            if sleep_dur > 0:
                time.sleep(sleep_dur)
        if os.path.exists({str(hold_backup_gate_flag)!r}):
            with open({str(in_backup_marker)!r}, "w", encoding="utf-8") as mf:
                mf.write("1")
            deadline = time.monotonic() + 10.0
            while os.path.exists({str(hold_backup_gate_flag)!r}) and time.monotonic() < deadline:
                time.sleep(0.05)
        if os.path.exists({str(fail_wiki_backup_flag)!r}):
            sys.exit(17)
        env = dict(os.environ, DALTON_WIKI_DATA_DIR={str(src_wiki)!r})
        rc = subprocess.run([{py_exe!r}, "-c", script], env=env).returncode
        sys.exit(rc)
    if "pg_dump" in rest:
        sys.stdout.buffer.write(b"PGDMP-FAKE-BACKUP")
        sys.exit(0)
    if "pg_restore" in rest:
        if "--list" in rest:
            sys.stdout.write("1; 0 0 TABLE public users postgres\\n")
            sys.exit(0)
        if "-f" in rest:
            sys.stdout.write("")
            sys.exit(0)
        sys.exit(0)
    if "pg_isready" in rest:
        sys.exit(0)
    if "psql" in rest:
        if "is_superuser" in joined or "rolsuper" in joined:
            sys.stdout.write("on\\n")
        elif "pg_database" in joined:
            sys.stdout.write("postgres\\n")
        elif "pg_class" in joined:
            sys.stdout.write("0\\t0\\t0\\n")
        elif "to_regclass" in joined:
            sys.stdout.write("\\n")
        elif "uploading" in joined:
            sys.stdout.write("0\\n")
        elif "alembic_version_dalton" in joined:
            sys.stdout.write("d049\\n")
        elif "alembic_version" in joined:
            sys.stdout.write("148\\n")
        sys.exit(0)
    if "backend" in rest and "python" in rest:
        # MinIO backup stream: empty valid tar.gz containing _dalton_minio_manifest.json
        manifest = {{
            "primary_bucket": "chat-uploads",
            "primary_key_prefix": "",
            "targets": {{"chat_upload": {{"bucket": "chat-uploads", "key_prefix": ""}}}},
            "bucket_count": 1,
            "object_count": 0,
            "total_bytes": 0,
            "objects": [],
        }}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            raw = json.dumps(manifest).encode("utf-8")
            ti = tarfile.TarInfo(name="_minio_manifest.json")
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
        sys.stdout.buffer.write(buf.getvalue())
        sys.exit(0)
    if "edge" in rest and "tar" in rest:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            raw = b"fake-caddy"
            ti = tarfile.TarInfo(name="caddy.txt")
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
        sys.stdout.buffer.write(buf.getvalue())
        sys.exit(0)
    if "curl" in rest:
        sys.exit(0)
    sys.exit(0)

if sub == "run":
    joined = " ".join(rest)
    if "research-host" in rest and "-c" in rest:
        c_idx = rest.index("-c")
        cmd_str = rest[c_idx + 1]
        extra = rest[c_idx + 2 :]
        if "tar -xzf" in cmd_str:
            rewritten = cmd_str.replace("/var/lib/dalton/wiki", {str(dst_wiki)!r})
            rc = subprocess.run(["/bin/sh", "-c", rewritten]).returncode
            sys.exit(rc)
        if "python -c" in cmd_str and len(extra) >= 2:
            env = dict(os.environ, DALTON_WIKI_DATA_DIR={str(dst_wiki)!r})
            rc = subprocess.run([{py_exe!r}, "-c", extra[1]], env=env).returncode
            sys.exit(rc)
    if "backend" in rest and "MINIO_PREFLIGHT" not in joined and "-c" in rest:
        if "EMPTY" in joined:
            sys.stdout.write("EMPTY\\n")
        sys.exit(0)
    sys.exit(0)

sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    fake_curl.chmod(0o755)

    backups_dir = tmp_path / "backups"
    run_env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    # 1. Run backup.sh
    b_res = subprocess.run(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(backups_dir),
        ],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert b_res.returncode == 0, f"backup stdout:\n{b_res.stdout}\nstderr:\n{b_res.stderr}"
    # r7/r8/r9 #1: Verify backup paused Wiki writes with owner+lease_id, drained in-flight writes, ran online backup, then resumed with same owner+lease_id
    events = pause_events_log.read_text(encoding="utf-8").splitlines()
    assert len(events) >= 4, events
    assert events[0].startswith("pause:") and events[1] == "poll_in_flight" and events[2] == "backup_python"
    assert events[-1] == "resume:" + events[0].split(":", 1)[1], events

    # r9 #1a: Copy duration EXCEEDS TTL (0.9s > 0.5s) -> heartbeat renews the SAME lease_id continuously and backup succeeds!
    pause_events_log.write_text("", encoding="utf-8")
    backup_sleep_file.write_text("0.9", encoding="utf-8")
    b_long_ok = subprocess.run(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(tmp_path / "backups_long_hb"),
        ],
        env={
            **run_env,
            "DALTON_WIKI_PAUSE_TTL_S": "0.5",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "0.15",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    backup_sleep_file.unlink()
    assert b_long_ok.returncode == 0, f"long heartbeat backup failed:\n{b_long_ok.stdout}\n{b_long_ok.stderr}"
    hb_events = pause_events_log.read_text(encoding="utf-8").splitlines()
    assert any(e.startswith("renew:") for e in hb_events), f"expected heartbeat renewals, got: {hb_events}"
    assert hb_events[-1].startswith("resume:"), hb_events

    # r9 #1b: Copy duration EXCEEDS TTL without timely heartbeat -> lease expires mid-copy ->
    # backup.sh MUST fail non-zero and NOT publish an inconsistent backup directory!
    pause_events_log.write_text("", encoding="utf-8")
    backup_sleep_file.write_text("0.45", encoding="utf-8")
    expired_out_dir = tmp_path / "backups_expired_lease"
    b_expired = subprocess.run(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(expired_out_dir),
        ],
        env={
            **run_env,
            "DALTON_WIKI_PAUSE_TTL_S": "0.15",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "5",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    backup_sleep_file.unlink()
    assert b_expired.returncode != 0, "backup.sh must fail when pause lease expires mid-backup"
    assert "expired" in b_expired.stderr.lower() or "did not remain active" in b_expired.stderr.lower()
    published_after_expiry = [p for p in expired_out_dir.iterdir() if p.is_dir() and p.name.startswith("dalton-backup-")] if expired_out_dir.exists() else []
    assert published_after_expiry == [], f"incomplete backup must be deleted on lease expiry, found: {published_after_expiry}"

    # r7 #1: Verify cleanup trap resumes Wiki writes even if online backup fails mid-stream
    pause_events_log.write_text("", encoding="utf-8")
    fail_wiki_backup_flag.write_text("1", encoding="utf-8")
    b_fail = subprocess.run(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(backups_dir),
        ],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    fail_wiki_backup_flag.unlink()
    assert b_fail.returncode != 0
    fail_events = pause_events_log.read_text(encoding="utf-8").splitlines()
    assert len(fail_events) >= 4 and fail_events[0].startswith("pause:") and fail_events[-1].startswith("resume:"), fail_events

    # r8 #1a: Resume failure MUST cause backup.sh to exit non-zero with a clear diagnostic (not just warn!)
    pause_events_log.write_text("", encoding="utf-8")
    fail_wiki_resume_flag.write_text("1", encoding="utf-8")
    b_res_fail = subprocess.run(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(tmp_path / "backups_resume_fail"),
        ],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    fail_wiki_resume_flag.unlink()
    # Reset simulated lease state since resume was forced to fail above
    if lease_state_file.exists():
        lease_state_file.unlink()
    assert b_res_fail.returncode != 0, "backup.sh must exit non-zero when resuming Wiki writes fails"
    assert "Failed to resume Wiki writes" in b_res_fail.stderr or "could not be confirmed resumed" in b_res_fail.stderr

    # r8/r9 #2: Atomic fcntl.flock per-instance Wiki mutex serializes / blocks concurrent backups (zero PID-file race!)
    import time

    pause_events_log.write_text("", encoding="utf-8")
    hold_backup_gate_flag.write_text("1", encoding="utf-8")
    if in_backup_marker.exists():
        in_backup_marker.unlink()
    p1 = subprocess.Popen(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(tmp_path / "backups_conc_1"),
        ],
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10.0
        while not in_backup_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert in_backup_marker.exists(), "First backup did not reach Wiki backup section"
        # Start second backup concurrently with wait=0.3s targeting a DIFFERENT output dir -> rejected by flock
        p2_res = subprocess.run(
            [
                "bash",
                str(backup_script),
                "--work-dir",
                str(work_dir),
                "--output-dir",
                str(tmp_path / "backups_conc_2"),
            ],
            env={**run_env, "DALTON_WIKI_BACKUP_LOCK_WAIT_S": "0.3"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert p2_res.returncode != 0, "Concurrent second backup must not bypass per-instance flock mutex"
        assert "Another Wiki backup is already running" in p2_res.stderr

        # Start third backup concurrently with generous wait -> waits until P1 releases flock, then succeeds serially!
        p3 = subprocess.Popen(
            [
                "bash",
                str(backup_script),
                "--work-dir",
                str(work_dir),
                "--output-dir",
                str(tmp_path / "backups_conc_3"),
            ],
            env={**run_env, "DALTON_WIKI_BACKUP_LOCK_WAIT_S": "15"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.25)
        assert p3.poll() is None, "p3 must block on flock while p1 holds it"
    finally:
        if hold_backup_gate_flag.exists():
            hold_backup_gate_flag.unlink()
        p1_out, p1_err = p1.communicate(timeout=20)
    p3_out, p3_err = p3.communicate(timeout=20)
    assert p1.returncode == 0, f"p1 failed:\n{p1_out}\n{p1_err}"
    assert p3.returncode == 0, f"p3 failed:\n{p3_out}\n{p3_err}"
    conc_events = pause_events_log.read_text(encoding="utf-8").splitlines()
    # Verify strict serialization: p1's resume MUST occur strictly BEFORE p3's pause!
    p1_resume_idx = next(idx for idx, ev in enumerate(conc_events) if ev.startswith("resume:"))
    p3_pause_idx = next(idx for idx, ev in enumerate(conc_events) if idx > 0 and ev.startswith("pause:"))
    assert p1_resume_idx < p3_pause_idx, f"Backups overlapped instead of serializing via flock: {conc_events}"

    backup_dirs = [p for p in backups_dir.iterdir() if p.is_dir() and p.name.startswith("dalton-backup-")]
    assert len(backup_dirs) == 1
    bdir = backup_dirs[0]

    wiki_tar = bdir / "wiki-data.tar.gz"
    assert wiki_tar.is_file()
    manifest = json.loads((bdir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["wiki"]["file"] == "wiki-data.tar.gz"
    assert manifest["wiki"]["file_count"] >= 5

    sums_text = (bdir / "SHA256SUMS").read_text(encoding="utf-8")
    wiki_sha = hashlib.sha256(wiki_tar.read_bytes()).hexdigest()
    assert f"{wiki_sha}  wiki-data.tar.gz" in sums_text
    with tarfile.open(wiki_tar, "r:gz") as tf:
        names = tf.getnames()
    assert "wiki/vectors.db" in names
    assert "state/wiki_ingest_audit.jsonl" in names
    assert "state/research-images/index.sqlite" in names
    assert not any(n.endswith(("-wal", "-shm", "-journal")) for n in names)

    # 2. Run restore.sh into empty dst_wiki
    r_res = subprocess.run(
        [
            "bash",
            str(restore_script),
            "--work-dir",
            str(work_dir),
            "--backup-dir",
            str(bdir),
            "--skip-edge-build",
        ],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r_res.returncode == 0, f"restore stdout:\n{r_res.stdout}\nstderr:\n{r_res.stderr}"

    # 3. Verify restored Wiki index, originals binary, image index, and audit receipts
    restored = OpenWikiStore(dst_wiki)
    search_res = restored.query("search", query="BackupRestoreUniqueCompressedPdfMarker")
    assert search_res["results"], f"expected restored PDF text hit, got: {search_res}"

    orig_rel = pdf_receipt["original_filepath"]
    assert (dst_wiki / orig_rel).read_bytes() == pdf_bytes

    sc_rimg, rimg_res = restored.read_image("image:1", allowed_sources={"company-wiki"})
    assert sc_rimg == 200 and rimg_res["ok"] is True

    # Check ingest_receipts table and audit JSONL match pre-backup
    with sqlite3.connect(str(dst_wiki / "wiki" / "vectors.db")) as conn:
        rcpt_rows = conn.execute(
            "SELECT request_id FROM ingest_receipts ORDER BY request_id"
        ).fetchall()
    assert [r[0] for r in rcpt_rows] == ["br-req-img-001", "br-req-pdf-001"]
    assert (dst_wiki / "state" / "wiki_ingest_audit.jsonl").read_text(encoding="utf-8") == (
        src_wiki / "state" / "wiki_ingest_audit.jsonl"
    ).read_text(encoding="utf-8")

    # 4. (r7 #2) Restore with a pre-3a .env lacking research-host tokens backfills BOTH distinct tokens
    from dotenv import dotenv_values

    (work_dir / ".env").write_text(
        "\n".join(
            [
                "COMPOSE_PROJECT_NAME=dalton_wiki_br_test",
                "DB_HOST=postgres",
                "DB_USER=postgres",
                "DB_PASSWORD=postgres",
                "DB_NAME=postgres",
                "DOMAIN=localhost",
                "DALTON_TLS_MODE=loopback",
                "DALTON_EDGE_PORT=8088",
                "DALTON_CHAT_UPLOAD_STORAGE_PROVIDER=s3",
                "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT=http://minio:9000",
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=chat-uploads",
                "MINIO_ROOT_USER=minioadmin",
                "MINIO_ROOT_PASSWORD=minioadmin",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    r_backfill = subprocess.run(
        [
            "bash",
            str(restore_script),
            "--work-dir",
            str(work_dir),
            "--backup-dir",
            str(bdir),
            "--skip-edge-build",
        ],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r_backfill.returncode == 0, f"restore backfill stderr:\n{r_backfill.stderr}"
    restored_env = dotenv_values(work_dir / ".env")
    rh_tok = restored_env.get("DALTON_RESEARCH_HOST_TOKEN") or ""
    rh_adm = restored_env.get("DALTON_RESEARCH_ADMIN_TOKEN") or ""
    assert len(rh_tok) == 64 and len(rh_adm) == 64 and rh_tok != rh_adm

    # 5. (r7 #1) Restoring an inconsistent Wiki snapshot (DB receipt missing from audit JSONL) MUST fail
    bad_bdir = tmp_path / "bad_backup_inconsistent"
    shutil.copytree(bdir, bad_bdir)
    # Rebuild wiki-data.tar.gz with an empty state/wiki_ingest_audit.jsonl (simulating mid-ingest drift)
    bad_stage = tmp_path / "bad_wiki_stage"
    bad_stage.mkdir()
    with tarfile.open(bad_bdir / "wiki-data.tar.gz", "r:gz") as tf_in:
        tf_in.extractall(bad_stage)
    (bad_stage / "state" / "wiki_ingest_audit.jsonl").write_text("", encoding="utf-8")
    with tarfile.open(bad_bdir / "wiki-data.tar.gz", "w:gz") as tf_out:
        for item in sorted(bad_stage.rglob("*")):
            tf_out.add(str(item), arcname=item.relative_to(bad_stage).as_posix(), recursive=False)
    # Recompute SHA256SUMS so restore.sh reaches Step 6 Wiki consistency verification
    new_sums: list[str] = []
    for line in (bad_bdir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) == 2 and parts[1] == "wiki-data.tar.gz":
            new_sha = hashlib.sha256((bad_bdir / "wiki-data.tar.gz").read_bytes()).hexdigest()
            new_sums.append(f"{new_sha}  wiki-data.tar.gz")
        else:
            new_sums.append(line)
    (bad_bdir / "SHA256SUMS").write_text("\n".join(new_sums) + "\n", encoding="utf-8")

    r_bad = subprocess.run(
        [
            "bash",
            str(restore_script),
            "--work-dir",
            str(work_dir),
            "--backup-dir",
            str(bad_bdir),
            "--skip-edge-build",
        ],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r_bad.returncode != 0, "restore.sh must fail when DB ingest_receipts lack matching audit JSONL"
    assert "wiki_ingest_audit.jsonl" in r_bad.stderr or "consistency" in r_bad.stderr.lower() or "verification" in r_bad.stderr.lower()

    def _run_tampered_wiki_restore(tag: str, mutator) -> subprocess.CompletedProcess[str]:
        t_bdir = tmp_path / f"bad_backup_{tag}"
        shutil.copytree(bdir, t_bdir)
        t_stage = tmp_path / f"bad_wiki_stage_{tag}"
        t_stage.mkdir()
        with tarfile.open(t_bdir / "wiki-data.tar.gz", "r:gz") as tf_in:
            tf_in.extractall(t_stage)
        mutator(t_stage)
        with tarfile.open(t_bdir / "wiki-data.tar.gz", "w:gz") as tf_out:
            for item in sorted(t_stage.rglob("*")):
                tf_out.add(str(item), arcname=item.relative_to(t_stage).as_posix(), recursive=False)
        recomputed: list[str] = []
        for line in (t_bdir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            parts = line.split("  ", 1)
            if len(parts) == 2 and parts[1] == "wiki-data.tar.gz":
                recomputed.append(f"{hashlib.sha256((t_bdir / 'wiki-data.tar.gz').read_bytes()).hexdigest()}  wiki-data.tar.gz")
            else:
                recomputed.append(line)
        (t_bdir / "SHA256SUMS").write_text("\n".join(recomputed) + "\n", encoding="utf-8")
        return subprocess.run(
            [
                "bash",
                str(restore_script),
                "--work-dir",
                str(work_dir),
                "--backup-dir",
                str(t_bdir),
                "--skip-edge-build",
            ],
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )

    # 6. (r8 #2a) False-pass guard: audit JSONL + markdown exist, but wiki/vectors.db is MISSING -> MUST fail
    r_missing_db = _run_tampered_wiki_restore(
        "missing_vectors_db",
        lambda st: (st / "wiki" / "vectors.db").unlink(),
    )
    assert r_missing_db.returncode != 0, "restore.sh must fail when audit records/markdown exist without vectors.db"
    assert "vectors.db" in r_missing_db.stderr

    # 7. (r8 #2b) False-pass guard: audit entry keeps same doc_id ('wiki:1') but has DIFFERENT request_id AND sha256 -> MUST fail
    def _corrupt_audit_req_and_sha(st: Path) -> None:
        audit_f = st / "state" / "wiki_ingest_audit.jsonl"
        mutated_lines: list[str] = []
        for raw_line in audit_f.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            obj = json.loads(raw_line)
            obj["request_id"] = "wrong-req-id-999"
            obj["sha256"] = "0" * 64
            mutated_lines.append(json.dumps(obj, ensure_ascii=False))
        audit_f.write_text("\n".join(mutated_lines) + "\n", encoding="utf-8")

    r_mismatch = _run_tampered_wiki_restore("req_sha_mismatch", _corrupt_audit_req_and_sha)
    assert r_mismatch.returncode != 0, "restore.sh must fail when audit request_id & sha256 differ even if doc_id matches"
    assert "mismatch" in r_mismatch.stderr.lower() or "verification" in r_mismatch.stderr.lower()

    # 8. (r9 #3) False-pass guard: audit JSONL has successful ingests, but ingest_receipts table is EMPTY -> MUST fail
    def _empty_ingest_receipts(st: Path) -> None:
        with sqlite3.connect(str(st / "wiki" / "vectors.db")) as conn:
            conn.execute("DELETE FROM ingest_receipts")
            conn.commit()

    r_empty_rcpts = _run_tampered_wiki_restore("empty_ingest_receipts", _empty_ingest_receipts)
    assert r_empty_rcpts.returncode != 0, "restore.sh must fail when ingest_receipts table is empty despite successful audit records"
    assert "ingest_receipts" in r_empty_rcpts.stderr


def _build_r11_backup_fixture(
    tmp_path: Path,
    *,
    project_name: str = "dalton_r11_test",
) -> dict[str, object]:
    """Create a self-contained backup.sh fixture with controllable fake docker & barriers."""
    import sys

    work_dir = tmp_path / "work"
    (work_dir / "scripts" / "standalone").mkdir(parents=True)
    for name in ("backup.sh", "storage_config.py", "env_tokens.sh"):
        src = _REPO_ROOT / "scripts" / "standalone" / name
        dst = work_dir / "scripts" / "standalone" / name
        shutil.copy2(src, dst)
        if name.endswith(".sh"):
            dst.chmod(0o755)
    (work_dir / "docker-compose.yml").write_text("services:\n  postgres: {}\n", encoding="utf-8")
    (work_dir / "docker-compose.core.yml").write_text("services:\n  research-host: {}\n", encoding="utf-8")
    (work_dir / ".env").write_text(
        "\n".join(
            [
                f"COMPOSE_PROJECT_NAME={project_name}",
                "DB_HOST=postgres",
                "DB_USER=postgres",
                "DB_PASSWORD=postgres",
                "DB_NAME=postgres",
                "DOMAIN=localhost",
                "DALTON_TLS_MODE=loopback",
                "DALTON_EDGE_PORT=8088",
                "DALTON_CHAT_UPLOAD_STORAGE_PROVIDER=s3",
                "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT=http://minio:9000",
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME=chat-uploads",
                "MINIO_ROOT_USER=minioadmin",
                "MINIO_ROOT_PASSWORD=minioadmin",
                "DALTON_RESEARCH_HOST_TOKEN=r11-mcp-token",
                "DALTON_RESEARCH_ADMIN_TOKEN=r11-admin-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (work_dir / ".env").chmod(0o600)

    src_wiki = tmp_path / "wiki_data"
    (src_wiki / "wiki").mkdir(parents=True)
    (src_wiki / "wiki" / "note.md").write_text("# r11 test\n", encoding="utf-8")

    state_dir = tmp_path / "sim_state"
    state_dir.mkdir()
    events_log = state_dir / "events.log"
    lease_state = state_dir / "lease.json"
    drain_polls_file = state_dir / "drain_polls.txt"
    drain_busy_until_file = state_dir / "drain_busy_until.txt"
    hold_backup_gate = state_dir / "hold_backup.gate"
    backup_entered_marker = state_dir / "backup_entered.marker"
    wait_for_renew_marker = state_dir / "wait_for_renew.flag"
    renew_entered_marker = state_dir / "renew_entered.marker"
    hang_renew_gate = state_dir / "hang_renew.gate"
    renew_cmd_log = state_dir / "renew_cmd.log"
    clock_file = state_dir / "clock.txt"
    fd_audit_log = state_dir / "fd_audit.log"
    hang_pause_after_apply_gate = state_dir / "hang_pause_after_apply.gate"
    pause_entered_marker = state_dir / "pause_entered.marker"
    hang_drain_gate = state_dir / "hang_drain.gate"
    drain_entered_marker = state_dir / "drain_entered.marker"
    hang_resume_gate = state_dir / "hang_resume.gate"
    resume_entered_marker = state_dir / "resume_entered.marker"
    hang_resume_first_only_flag = state_dir / "hang_resume_first_only.flag"
    resume_attempts_file = state_dir / "resume_attempts.txt"
    probe_delay_until_file = state_dir / "probe_delay_until.txt"
    probe_attempts_file = state_dir / "probe_attempts.txt"
    drain_stdout_holder_flag = state_dir / "drain_stdout_holder.flag"
    drain_stdout_holder_pid_file = state_dir / "drain_stdout_holder.pid"
    drain_detached_child_flag = state_dir / "drain_detached_child.flag"
    drain_detached_child_pid_file = state_dir / "drain_detached_child.pid"
    pause_fail_exit_code_file = state_dir / "pause_fail_exit_code.txt"
    probe_hang_first_only_flag = state_dir / "probe_hang_first_only.flag"
    probe_hang_entered_marker = state_dir / "probe_hang_entered.marker"
    resume_bad_output_first_only_file = state_dir / "resume_bad_output_first_only.txt"
    spawn_sigterm_ignoring_child_flag = state_dir / "spawn_sigterm_child.flag"
    sigterm_ignoring_child_pid_file = state_dir / "sigterm_child.pid"
    sigterm_ignoring_child_ready_file = state_dir / "sigterm_child.ready"

    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    py_exe = sys.executable

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!{py_exe}
import fcntl, io, json, os, signal, subprocess, sys, tarfile, time

args = sys.argv[1:]
if args and args[0] == "compose":
    args = args[1:]
i = 0
while i < len(args) and args[i].startswith("-"):
    if args[i] in ("-p", "-f", "--env-file", "--project-directory"):
        i += 2
    else:
        i += 1
if i >= len(args):
    sys.exit(0)
sub = args[i]
rest = args[i + 1 :]
joined = " ".join(rest)

if sub == "config" and "--services" in rest:
    sys.stdout.write("postgres\\nredis\\nminio\\nbackend\\nedge\\nresearch-host\\n")
    sys.exit(0)

if sub == "ps":
    sys.stdout.write("postgres\\nredis\\nminio\\nbackend\\nedge\\nresearch-host\\n")
    sys.exit(0)

if sub == "exec":
    if "research-host" in rest:
        leaked_fds = []
        for check_fd in (8, 9):
            try:
                os.fstat(check_fd)
                leaked_fds.append(check_fd)
            except OSError:
                pass
        with open({str(fd_audit_log)!r}, "a", encoding="utf-8") as faf:
            faf.write(f"fds={{leaked_fds}}\\n")
    if "research-host" in rest and "/admin/wiki/pause-writes" in joined:
        tok = os.environ.get("DALTON_RH_ADMIN_TOKEN", "")
        if not tok:
            sys.exit(1)
        owner = os.environ.get("DALTON_RH_PAUSE_OWNER", "")
        lease_id_env = os.environ.get("DALTON_RH_PAUSE_LEASE_ID", "").strip()
        ttl_env = os.environ.get("DALTON_RH_PAUSE_TTL_S", "").strip()
        expect_owner = os.environ.get("DALTON_RH_EXPECT_OWNER", "")
        expect_lid = os.environ.get("DALTON_RH_EXPECT_LEASE_ID", "").strip()
        probe_owner = os.environ.get("DALTON_RH_PROBE_OWNER", "")
        state_path = {str(lease_state)!r}

        def _now():
            if os.path.exists({str(clock_file)!r}):
                with open({str(clock_file)!r}, encoding="utf-8") as cf:
                    return float((cf.read().strip() or "0"))
            return time.time()

        def _load_state():
            if os.path.exists(state_path):
                with open(state_path, encoding="utf-8") as sf:
                    st = json.load(sf)
            else:
                st = {{"next_id": 0, "paused": False, "owner": None, "lease_id": 0, "expires_at": 0.0}}
            now = _now()
            if st["paused"] and now >= float(st.get("expires_at") or 0.0):
                st["paused"] = False
                st["owner"] = None
                st["lease_id"] = 0
            return st, now

        if "DALTON_RH_PAUSE_TTL_S" in joined and "DALTON_RH_PAUSE_LEASE_ID" in joined:
            # Heartbeat renewal
            with open({str(renew_cmd_log)!r}, "a", encoding="utf-8") as cf:
                cf.write(joined + "\\n")
            if os.path.exists({str(spawn_sigterm_ignoring_child_flag)!r}):
                # Leader waits on ready barrier proving grandchild already installed SIG_IGN for SIGTERM,
                # then leader exits immediately on SIGTERM!
                ready_p = {str(sigterm_ignoring_child_ready_file)!r}
                child = subprocess.Popen(
                    [
                        {py_exe!r},
                        "-c",
                        "import os, signal, sys, time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
                        "time.sleep(30)",
                        ready_p,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with open({str(sigterm_ignoring_child_pid_file)!r}, "w", encoding="utf-8") as cpf:
                    cpf.write(str(child.pid))
                ready_deadline = time.monotonic() + 5.0
                while not os.path.exists(ready_p) and time.monotonic() < ready_deadline:
                    time.sleep(0.01)
                signal.signal(signal.SIGTERM, lambda _s, _f: os._exit(0))
            if os.path.exists({str(hang_renew_gate)!r}):
                with open({str(renew_entered_marker)!r}, "w", encoding="utf-8") as mf:
                    mf.write(str(os.getpid()))
                deadline = time.monotonic() + 30.0
                while os.path.exists({str(hang_renew_gate)!r}) and time.monotonic() < deadline:
                    time.sleep(0.05)
            lock_fd = os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                st, now = _load_state()
                req_lid = int(lease_id_env)
                ttl_s = float(ttl_env or "300")
                with open({str(events_log)!r}, "a", encoding="utf-8") as lf:
                    if not st["paused"] or st["lease_id"] != req_lid or st["owner"] != owner:
                        lf.write(f"renew_expired:{{os.getppid()}}:{{owner}}:{{req_lid}}\\n")
                        with open(state_path, "w", encoding="utf-8") as sf:
                            json.dump(st, sf)
                        sys.exit(1)
                    st["expires_at"] = now + ttl_s
                    lf.write(f"renew:{{os.getppid()}}:{{owner}}:{{req_lid}}\\n")
                    with open(state_path, "w", encoding="utf-8") as sf:
                        json.dump(st, sf)
                    sys.exit(0)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

        if "DALTON_RH_PROBE_OWNER" in joined:
            p_cnt = 0
            lock_fd = os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if os.path.exists({str(probe_attempts_file)!r}):
                    with open({str(probe_attempts_file)!r}, encoding="utf-8") as paf:
                        p_cnt = int((paf.read().strip() or "0"))
                p_cnt += 1
                with open({str(probe_attempts_file)!r}, "w", encoding="utf-8") as paf:
                    paf.write(str(p_cnt))
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if p_cnt == 1 and os.path.exists({str(probe_hang_first_only_flag)!r}):
                with open({str(probe_hang_entered_marker)!r}, "w", encoding="utf-8") as mf:
                    mf.write(str(os.getpid()))
                deadline = time.monotonic() + 30.0
                while os.path.exists({str(probe_hang_first_only_flag)!r}) and time.monotonic() < deadline:
                    time.sleep(0.05)
            lock_fd = os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                delay_until = 0
                if os.path.exists({str(probe_delay_until_file)!r}):
                    with open({str(probe_delay_until_file)!r}, encoding="utf-8") as pdf:
                        delay_until = int((pdf.read().strip() or "0"))
                st, _now_val = _load_state()
                if p_cnt <= delay_until:
                    # Simulate probe arriving BEFORE delayed pause POST took effect
                    with open({str(events_log)!r}, "a", encoding="utf-8") as lf:
                        lf.write(f"probe_pause:{{probe_owner}}:NONE:attempt={{p_cnt}}\\n")
                    sys.stdout.write("NONE\\n")
                    sys.exit(0)
                lid = st["lease_id"] if (st["paused"] is True and str(st.get("owner") or "") == probe_owner) else ""
                with open({str(events_log)!r}, "a", encoding="utf-8") as lf:
                    lf.write(f"probe_pause:{{probe_owner}}:{{lid or 'NONE'}}:attempt={{p_cnt}}\\n")
                sys.stdout.write(f"{{lid if lid else 'NONE'}}\\n")
                sys.exit(0)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

        if "DALTON_RH_PAUSE_TTL_S" in joined:
            lock_fd = os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                st, now = _load_state()
                ttl_s = float(ttl_env or "300")
                st["next_id"] += 1
                st["paused"] = True
                st["owner"] = owner
                st["lease_id"] = st["next_id"]
                st["expires_at"] = now + ttl_s
                lid = st["lease_id"]
                with open({str(events_log)!r}, "a", encoding="utf-8") as lf:
                    lf.write(f"pause:{{owner}}:{{lid}}\\n")
                with open(state_path, "w", encoding="utf-8") as sf:
                    json.dump(st, sf)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            if os.path.exists({str(pause_fail_exit_code_file)!r}):
                fail_rc = int((open({str(pause_fail_exit_code_file)!r}, encoding="utf-8").read().strip() or "52"))
                sys.exit(fail_rc)
            if os.path.exists({str(hang_pause_after_apply_gate)!r}):
                with open({str(pause_entered_marker)!r}, "w", encoding="utf-8") as mf:
                    mf.write(str(os.getpid()))
                deadline = time.monotonic() + 30.0
                while os.path.exists({str(hang_pause_after_apply_gate)!r}) and time.monotonic() < deadline:
                    time.sleep(0.05)
            sys.stdout.write(f"{{lid}}\\n")
            sys.exit(0)

        if "DALTON_RH_PAUSE_OWNER" in joined:
            r_cnt = 0
            if os.path.exists({str(resume_attempts_file)!r}):
                with open({str(resume_attempts_file)!r}, encoding="utf-8") as raf:
                    r_cnt = int((raf.read().strip() or "0"))
            r_cnt += 1
            with open({str(resume_attempts_file)!r}, "w", encoding="utf-8") as raf:
                raf.write(str(r_cnt))
            if r_cnt == 1 and os.path.exists({str(resume_bad_output_first_only_file)!r}):
                bad_out = open({str(resume_bad_output_first_only_file)!r}, encoding="utf-8").read().strip()
                with open({str(events_log)!r}, "a", encoding="utf-8") as lf:
                    lf.write(f"resume_bad_output:{{owner}}:{{bad_out}}\\n")
                sys.stdout.write(bad_out + "\\n")
                sys.exit(0)
            should_hang_resume = os.path.exists({str(hang_resume_gate)!r}) and (
                (not os.path.exists({str(hang_resume_first_only_flag)!r})) or r_cnt == 1
            )
            if should_hang_resume:
                with open({str(resume_entered_marker)!r}, "w", encoding="utf-8") as mf:
                    mf.write(str(os.getpid()))
                deadline = time.monotonic() + 30.0
                while os.path.exists({str(hang_resume_gate)!r}) and time.monotonic() < deadline:
                    time.sleep(0.05)
            lock_fd = os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                st, _now_val = _load_state()
                req_lid = int(lease_id_env) if lease_id_env else 0
                with open({str(events_log)!r}, "a", encoding="utf-8") as lf:
                    if not st["paused"]:
                        lf.write(f"resume_expired:{{owner}}:{{req_lid}}\\n")
                        with open(state_path, "w", encoding="utf-8") as sf:
                            json.dump(st, sf)
                        sys.stdout.write("EXPIRED\\n")
                        sys.exit(0)
                    st["paused"] = False
                    st["owner"] = None
                    st["lease_id"] = 0
                    st["expires_at"] = 0.0
                    lf.write(f"resume:{{owner}}:{{req_lid}}\\n")
                    with open(state_path, "w", encoding="utf-8") as sf:
                        json.dump(st, sf)
                    sys.stdout.write("OK\\n")
                    sys.exit(0)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

        # GET /admin/wiki/pause-writes drain poll with lease validation
        if os.path.exists({str(drain_detached_child_flag)!r}):
            # Group leader spawns a descendant in the SAME process group that CLOSES stdout (DEVNULL)
            # and keeps running, then leader writes "0" and exits 0 immediately!
            detached = subprocess.Popen(
                [
                    {py_exe!r},
                    "-c",
                    "import os, sys, time; "
                    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
                    "time.sleep(30)",
                    {str(drain_detached_child_pid_file)!r},
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ready_deadline = time.monotonic() + 5.0
            while not os.path.exists({str(drain_detached_child_pid_file)!r}) and time.monotonic() < ready_deadline:
                time.sleep(0.01)
            sys.stdout.write("0\\n")
            sys.stdout.flush()
            os._exit(0)
        if os.path.exists({str(drain_stdout_holder_flag)!r}):
            # Group leader spawns a descendant that inherits stdout and blocks, then leader exits 0 immediately!
            holder = subprocess.Popen(
                [
                    {py_exe!r},
                    "-c",
                    "import os, sys, time; "
                    "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid())); "
                    "time.sleep(30)",
                    {str(drain_stdout_holder_pid_file)!r},
                ],
                stdin=subprocess.DEVNULL,
                stdout=None,  # explicitly inherit stdout pipe!
                stderr=subprocess.DEVNULL,
            )
            ready_deadline = time.monotonic() + 5.0
            while not os.path.exists({str(drain_stdout_holder_pid_file)!r}) and time.monotonic() < ready_deadline:
                time.sleep(0.01)
            sys.stdout.write("1\\n")
            sys.stdout.flush()
            os._exit(0)
        if os.path.exists({str(hang_drain_gate)!r}):
            with open({str(drain_entered_marker)!r}, "w", encoding="utf-8") as mf:
                mf.write(str(os.getpid()))
            deadline = time.monotonic() + 30.0
            while os.path.exists({str(hang_drain_gate)!r}) and time.monotonic() < deadline:
                time.sleep(0.05)
        lock_fd = os.open(state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            st, _now_val = _load_state()
            poll_cnt = 0
            if os.path.exists({str(drain_polls_file)!r}):
                with open({str(drain_polls_file)!r}, encoding="utf-8") as pf:
                    poll_cnt = int((pf.read().strip() or "0"))
            poll_cnt += 1
            with open({str(drain_polls_file)!r}, "w", encoding="utf-8") as pf:
                pf.write(str(poll_cnt))
            busy_until = 0
            if os.path.exists({str(drain_busy_until_file)!r}):
                with open({str(drain_busy_until_file)!r}, encoding="utf-8") as bf:
                    busy_until = int((bf.read().strip() or "0"))
            lease_ok = (
                st["paused"] is True
                and (not expect_owner or str(st.get("owner") or "") == expect_owner)
                and (not expect_lid or str(st.get("lease_id") or "") == expect_lid)
            )
            with open({str(events_log)!r}, "a", encoding="utf-8") as lf:
                if not lease_ok:
                    lf.write(f"poll_lease_lost:{{poll_cnt}}\\n")
                    sys.stdout.write("LEASE_LOST\\n")
                    sys.exit(0)
                in_flight = 1 if poll_cnt <= busy_until else 0
                lf.write(f"poll_in_flight:{{poll_cnt}}:{{in_flight}}\\n")
                sys.stdout.write(f"{{in_flight}}\\n")
                sys.exit(0)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    if "research-host" in rest and "-c" in rest:
        idx = rest.index("-c")
        script = rest[idx + 1]
        with open({str(events_log)!r}, "a", encoding="utf-8") as lf:
            lf.write("backup_python\\n")
        with open({str(backup_entered_marker)!r}, "w", encoding="utf-8") as mf:
            mf.write(str(os.getpid()))
        if os.path.exists({str(wait_for_renew_marker)!r}):
            deadline = time.monotonic() + 10.0
            while not os.path.exists({str(renew_entered_marker)!r}) and time.monotonic() < deadline:
                time.sleep(0.02)
            extra_wait = float((open({str(wait_for_renew_marker)!r}).read().strip() or "0"))
            if extra_wait > 0:
                time.sleep(extra_wait)
        if os.path.exists({str(hold_backup_gate)!r}):
            deadline = time.monotonic() + 15.0
            while os.path.exists({str(hold_backup_gate)!r}) and time.monotonic() < deadline:
                time.sleep(0.05)
        env = dict(os.environ, DALTON_WIKI_DATA_DIR={str(src_wiki)!r})
        rc = subprocess.run([{py_exe!r}, "-c", script], env=env).returncode
        sys.exit(rc)

    if "pg_dump" in rest:
        sys.stdout.buffer.write(b"PGDMP-FAKE-BACKUP")
        sys.exit(0)
    if "pg_restore" in rest:
        if "--list" in rest:
            sys.stdout.write("1; 0 0 TABLE public users postgres\\n")
        sys.exit(0)
    if "psql" in rest:
        if "alembic_version_dalton" in joined:
            sys.stdout.write("d049\\n")
        elif "alembic_version" in joined:
            sys.stdout.write("148\\n")
        sys.exit(0)
    if "backend" in rest and "python" in rest:
        manifest = {{
            "primary_bucket": "chat-uploads",
            "primary_key_prefix": "",
            "targets": {{"chat_upload": {{"bucket": "chat-uploads", "key_prefix": ""}}}},
            "bucket_count": 1,
            "object_count": 0,
            "total_bytes": 0,
            "objects": [],
        }}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            raw = json.dumps(manifest).encode("utf-8")
            ti = tarfile.TarInfo(name="_minio_manifest.json")
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
        sys.stdout.buffer.write(buf.getvalue())
        sys.exit(0)
    if "edge" in rest and "tar" in rest:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            raw = b"fake-caddy"
            ti = tarfile.TarInfo(name="caddy.txt")
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
        sys.stdout.buffer.write(buf.getvalue())
        sys.exit(0)
    sys.exit(0)

sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    (fake_bin / "curl").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (fake_bin / "curl").chmod(0o755)

    return {
        "work_dir": work_dir,
        "backup_script": work_dir / "scripts" / "standalone" / "backup.sh",
        "fake_bin": fake_bin,
        "env": {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        "events_log": events_log,
        "lease_state": lease_state,
        "drain_polls_file": drain_polls_file,
        "drain_busy_until_file": drain_busy_until_file,
        "hold_backup_gate": hold_backup_gate,
        "backup_entered_marker": backup_entered_marker,
        "wait_for_renew_marker": wait_for_renew_marker,
        "renew_entered_marker": renew_entered_marker,
        "hang_renew_gate": hang_renew_gate,
        "renew_cmd_log": renew_cmd_log,
        "clock_file": clock_file,
        "fd_audit_log": fd_audit_log,
        "hang_pause_after_apply_gate": hang_pause_after_apply_gate,
        "pause_entered_marker": pause_entered_marker,
        "hang_drain_gate": hang_drain_gate,
        "drain_entered_marker": drain_entered_marker,
        "hang_resume_gate": hang_resume_gate,
        "resume_entered_marker": resume_entered_marker,
        "hang_resume_first_only_flag": hang_resume_first_only_flag,
        "resume_attempts_file": resume_attempts_file,
        "probe_delay_until_file": probe_delay_until_file,
        "probe_attempts_file": probe_attempts_file,
        "drain_stdout_holder_flag": drain_stdout_holder_flag,
        "drain_stdout_holder_pid_file": drain_stdout_holder_pid_file,
        "drain_detached_child_flag": drain_detached_child_flag,
        "drain_detached_child_pid_file": drain_detached_child_pid_file,
        "pause_fail_exit_code_file": pause_fail_exit_code_file,
        "probe_hang_first_only_flag": probe_hang_first_only_flag,
        "probe_hang_entered_marker": probe_hang_entered_marker,
        "resume_bad_output_first_only_file": resume_bad_output_first_only_file,
        "spawn_sigterm_ignoring_child_flag": spawn_sigterm_ignoring_child_flag,
        "sigterm_ignoring_child_pid_file": sigterm_ignoring_child_pid_file,
        "sigterm_ignoring_child_ready_file": sigterm_ignoring_child_ready_file,
    }


def test_r11_backup_heartbeat_exits_when_parent_killed_with_sigkill(tmp_path: Path) -> None:
    """r11 #1: When main backup.sh is SIGKILL'd, background heartbeat exits within one cycle and stops renewing."""
    import signal
    import time

    fx = _build_r11_backup_fixture(tmp_path, project_name="dalton_r11_orphan")
    fx["hold_backup_gate"].write_text("1", encoding="utf-8")
    lock_file = tmp_path / "orphan.flock"

    proc = subprocess.Popen(
        [
            "bash",
            str(fx["backup_script"]),
            "--work-dir",
            str(fx["work_dir"]),
            "--output-dir",
            str(tmp_path / "out_orphan"),
        ],
        env={
            **fx["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_file),
            "DALTON_WIKI_PAUSE_TTL_S": "1.0",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "0.15",
            "DALTON_WIKI_PAUSE_HEARTBEAT_TIMEOUT_S": "2.0",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    hb_pid: int | None = None
    try:
        # Wait until backup entered AND heartbeat completed at least one renewal
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if fx["backup_entered_marker"].exists() and fx["events_log"].exists():
                renews = [
                    ln
                    for ln in fx["events_log"].read_text(encoding="utf-8").splitlines()
                    if ln.startswith("renew:")
                ]
                if renews:
                    hb_pid = int(renews[-1].split(":")[1])
                    break
            time.sleep(0.03)
        assert hb_pid is not None, "Heartbeat did not perform initial renewal before SIGKILL"

        # Simulate OOM / kill -9 of main backup.sh process
        t_kill = time.monotonic()
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)

        # Heartbeat must detect parent death and exit within one heartbeat cycle (<= 0.5s)
        hb_dead = False
        exit_deadline = t_kill + 0.6
        while time.monotonic() < exit_deadline:
            try:
                os.kill(hb_pid, 0)
            except ProcessLookupError:
                hb_dead = True
                break
            time.sleep(0.02)
        assert hb_dead, f"Orphan heartbeat PID {hb_pid} stayed alive after parent SIGKILL"

        renews_after_exit = [
            ln
            for ln in fx["events_log"].read_text(encoding="utf-8").splitlines()
            if ln.startswith("renew:")
        ]
        # Wait 2 more heartbeat cycles and confirm zero further renewals occur
        time.sleep(0.35)
        renews_later = [
            ln
            for ln in fx["events_log"].read_text(encoding="utf-8").splitlines()
            if ln.startswith("renew:")
        ]
        assert len(renews_later) == len(renews_after_exit), (
            f"Orphan heartbeat kept renewing after parent death: before={renews_after_exit}, after={renews_later}"
        )
    finally:
        if fx["hold_backup_gate"].exists():
            fx["hold_backup_gate"].unlink()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if hb_pid is not None:
            try:
                os.kill(hb_pid, signal.SIGKILL)
            except OSError:
                pass


def test_r11_backup_lock_handshake_reports_unopenable_lock_and_times_out_without_hanging(
    tmp_path: Path,
) -> None:
    """r11 #2: Lock handshake never hangs when lock file cannot be opened or lock helper stalls."""
    import sys
    import time

    fx = _build_r11_backup_fixture(tmp_path, project_name="dalton_r11_lock")

    # 2a. Unopenable lock file (0o000 simulates root-owned 0600 file left by sudo)
    unreadable_lock = tmp_path / "root_owned_sim.flock"
    unreadable_lock.write_text("", encoding="utf-8")
    unreadable_lock.chmod(0o000)
    try:
        t0 = time.monotonic()
        res_perm = subprocess.run(
            [
                "bash",
                str(fx["backup_script"]),
                "--work-dir",
                str(fx["work_dir"]),
                "--output-dir",
                str(tmp_path / "out_perm"),
            ],
            env={
                **fx["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(unreadable_lock),
                "DALTON_WIKI_BACKUP_LOCK_WAIT_S": "1",
                "DALTON_WIKI_BACKUP_LOCK_HANDSHAKE_TIMEOUT_S": "3",
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        elapsed_perm = time.monotonic() - t0
    finally:
        unreadable_lock.chmod(0o600)

    assert res_perm.returncode != 0
    assert elapsed_perm < 5.0, f"Lock handshake hung on unreadable lock file: {elapsed_perm:.2f}s"
    assert str(unreadable_lock) in res_perm.stderr
    assert "PermissionError" in res_perm.stderr or "Permission denied" in res_perm.stderr

    # 2b. Stalled lock helper (never writes status) hits total handshake timeout without hanging
    stall_bin = tmp_path / "stall_bin"
    stall_bin.mkdir()
    py_exe = sys.executable
    py_shim = stall_bin / "python3"
    py_shim.write_text(
        f"""#!{py_exe}
import os, sys, time
if len(sys.argv) >= 3 and sys.argv[1] == "-c" and "fcntl.flock" in sys.argv[2]:
    time.sleep(30)
    sys.exit(0)
os.execv({py_exe!r}, [{py_exe!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    py_shim.chmod(0o755)

    stall_lock = tmp_path / "stalled.flock"
    t1 = time.monotonic()
    res_stall = subprocess.run(
        [
            "bash",
            str(fx["backup_script"]),
            "--work-dir",
            str(fx["work_dir"]),
            "--output-dir",
            str(tmp_path / "out_stall"),
        ],
        env={
            **fx["env"],
            "PATH": f"{stall_bin}:{fx['env']['PATH']}",
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(stall_lock),
            "DALTON_WIKI_BACKUP_LOCK_WAIT_S": "0.1",
            "DALTON_WIKI_BACKUP_LOCK_HANDSHAKE_TIMEOUT_S": "0.4",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    elapsed_stall = time.monotonic() - t1
    assert res_stall.returncode != 0
    assert elapsed_stall < 5.0, f"Stalled lock helper handshake exceeded timeout: {elapsed_stall:.2f}s"
    assert str(stall_lock) in res_stall.stderr
    assert "handshake" in res_stall.stderr.lower()


def test_r11_backup_heartbeat_request_timeout_and_stop_reaps_hung_exec(tmp_path: Path) -> None:
    """r11 #3: Heartbeat enforces curl & compose exec timeouts, and stop_wiki_pause_heartbeat terminates/reaps in-flight child."""
    import time
    import pytest

    # 3a. Hung renewal subprocess exceeds DALTON_WIKI_PAUSE_HEARTBEAT_TIMEOUT_S -> killed, marked failed, backup aborted
    fx_to = _build_r11_backup_fixture(tmp_path / "timeout_case", project_name="dalton_r11_hbto")
    fx_to["hang_renew_gate"].write_text("1", encoding="utf-8")
    fx_to["wait_for_renew_marker"].write_text("0.45", encoding="utf-8")
    t0 = time.monotonic()
    try:
        res_to = subprocess.run(
            [
                "bash",
                str(fx_to["backup_script"]),
                "--work-dir",
                str(fx_to["work_dir"]),
                "--output-dir",
                str(tmp_path / "timeout_case" / "out"),
            ],
            env={
                **fx_to["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(tmp_path / "timeout_case" / "hbto.flock"),
                "DALTON_WIKI_PAUSE_TTL_S": "5.0",
                "DALTON_WIKI_PAUSE_HEARTBEAT_S": "0.05",
                "DALTON_WIKI_PAUSE_HEARTBEAT_TIMEOUT_S": "0.2",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        if fx_to["hang_renew_gate"].exists():
            fx_to["hang_renew_gate"].unlink()
    elapsed_to = time.monotonic() - t0
    assert res_to.returncode != 0, "backup.sh must fail when heartbeat renewal times out"
    assert elapsed_to < 8.0, f"Heartbeat timeout did not bound backup duration: {elapsed_to:.2f}s"
    renew_cmd = fx_to["renew_cmd_log"].read_text(encoding="utf-8")
    assert "--connect-timeout" in renew_cmd and "--max-time" in renew_cmd
    hung_pid = int(fx_to["renew_entered_marker"].read_text(encoding="utf-8").strip())
    # Hung child must already have been killed & reaped by the timeout handler
    with pytest.raises(ProcessLookupError):
        os.kill(hung_pid, 0)

    # 3b. stop_wiki_pause_heartbeat called while renewal exec is blocked -> kills & reaps child without wait hanging
    fx_stop = _build_r11_backup_fixture(tmp_path / "stop_case", project_name="dalton_r11_hbstop")
    fx_stop["hang_renew_gate"].write_text("1", encoding="utf-8")
    # Backup exits immediately once renewal child enters its blocked wait
    fx_stop["wait_for_renew_marker"].write_text("0", encoding="utf-8")
    t1 = time.monotonic()
    try:
        res_stop = subprocess.run(
            [
                "bash",
                str(fx_stop["backup_script"]),
                "--work-dir",
                str(fx_stop["work_dir"]),
                "--output-dir",
                str(tmp_path / "stop_case" / "out"),
            ],
            env={
                **fx_stop["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(tmp_path / "stop_case" / "hbstop.flock"),
                "DALTON_WIKI_PAUSE_TTL_S": "10.0",
                "DALTON_WIKI_PAUSE_HEARTBEAT_S": "0.05",
                "DALTON_WIKI_PAUSE_HEARTBEAT_TIMEOUT_S": "15.0",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        if fx_stop["hang_renew_gate"].exists():
            fx_stop["hang_renew_gate"].unlink()
    elapsed_stop = time.monotonic() - t1
    assert res_stop.returncode == 0, f"backup failed:\n{res_stop.stdout}\n{res_stop.stderr}"
    assert elapsed_stop < 6.0, f"stop_wiki_pause_heartbeat hung waiting for in-flight renewal: {elapsed_stop:.2f}s"
    stopped_pid = int(fx_stop["renew_entered_marker"].read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(stopped_pid, 0)


def test_r11_backup_heartbeat_starts_before_drain_and_aborts_on_lease_loss_during_drain(
    tmp_path: Path,
) -> None:
    """r11 #4: Heartbeat starts right after pause (surviving slow drain > TTL), and lease loss during drain aborts & resumes."""
    # 4a. Slow drain lasts ~0.6s (> TTL=0.45s); heartbeat starts immediately after pause and renews during drain
    fx_ok = _build_r11_backup_fixture(tmp_path / "drain_ok", project_name="dalton_r11_drainok")
    fx_ok["drain_busy_until_file"].write_text("3", encoding="utf-8")
    res_ok = subprocess.run(
        [
            "bash",
            str(fx_ok["backup_script"]),
            "--work-dir",
            str(fx_ok["work_dir"]),
            "--output-dir",
            str(tmp_path / "drain_ok" / "out"),
        ],
        env={
            **fx_ok["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(tmp_path / "drain_ok" / "drainok.flock"),
            "DALTON_WIKI_PAUSE_TTL_S": "0.45",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "0.12",
            "DALTON_WIKI_DRAIN_POLL_S": "0.2",
            "DALTON_WIKI_DRAIN_MAX_POLLS": "10",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_ok.returncode == 0, f"slow-drain backup failed:\n{res_ok.stdout}\n{res_ok.stderr}"
    ok_events = fx_ok["events_log"].read_text(encoding="utf-8").splitlines()
    backup_idx = ok_events.index("backup_python")
    pre_backup_renews = [ev for ev in ok_events[:backup_idx] if ev.startswith("renew:")]
    assert pre_backup_renews, f"Expected heartbeat renewals BEFORE backup_python during slow drain, got: {ok_events}"

    # 4b. Lease expires during drain -> drain poll detects LEASE_LOST, aborts before backup_python, and resumes writes
    fx_lost = _build_r11_backup_fixture(tmp_path / "drain_lost", project_name="dalton_r11_drainlost")
    fx_lost["drain_busy_until_file"].write_text("2", encoding="utf-8")
    res_lost = subprocess.run(
        [
            "bash",
            str(fx_lost["backup_script"]),
            "--work-dir",
            str(fx_lost["work_dir"]),
            "--output-dir",
            str(tmp_path / "drain_lost" / "out"),
        ],
        env={
            **fx_lost["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(tmp_path / "drain_lost" / "drainlost.flock"),
            "DALTON_WIKI_PAUSE_TTL_S": "0.12",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "5",
            "DALTON_WIKI_DRAIN_POLL_S": "0.22",
            "DALTON_WIKI_DRAIN_MAX_POLLS": "10",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_lost.returncode != 0, "backup.sh must abort when pause lease expires during in-flight drain"
    assert "no longer active while draining" in res_lost.stderr
    lost_events = fx_lost["events_log"].read_text(encoding="utf-8").splitlines()
    assert "backup_python" not in lost_events, f"backup_python must not run after lease lost during drain: {lost_events}"
    assert any(ev.startswith("resume") for ev in lost_events), f"Expected resume after drain lease loss: {lost_events}"


def test_r11_env_tokens_preserves_group_or_falls_back_to_0600(tmp_path: Path) -> None:
    """r11 #5: ensure_env_tokens preserves original .env group ownership (with 0640) and falls back to 0600 when chgrp fails."""
    import stat

    env_tokens_sh = _REPO_ROOT / "scripts" / "standalone" / "env_tokens.sh"

    # 5a. Group ownership preserved alongside 0640 mode
    env_keep = tmp_path / "keep.env"
    env_keep.write_text("DOMAIN=localhost\n", encoding="utf-8")
    groups = os.getgroups()
    alt_gid = next((g for g in groups if g != os.getgid()), os.getgid())
    os.chown(env_keep, -1, alt_gid)
    env_keep.chmod(0o640)

    res_keep = subprocess.run(
        ["bash", "-c", 'source "$1" && ensure_research_host_tokens "$2"', "_", str(env_tokens_sh), str(env_keep)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_keep.returncode == 0, res_keep.stderr
    assert "DALTON_RESEARCH_HOST_TOKEN" in res_keep.stdout
    assert "DALTON_RESEARCH_ADMIN_TOKEN" in res_keep.stdout
    st_keep = env_keep.stat()
    assert st_keep.st_gid == alt_gid
    assert stat.S_IMODE(st_keep.st_mode) == 0o640

    # 5b. When preserving group fails (os.chown raises EPERM), mode falls back to 0600
    env_fallback = tmp_path / "fallback.env"
    env_fallback.write_text("DOMAIN=localhost\n", encoding="utf-8")
    env_fallback.chmod(0o640)

    hook_dir = tmp_path / "py_hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        "import errno, os\n"
        "_orig_chown = os.chown\n"
        "def _deny_tmp_chown(path, uid, gid, *a, **kw):\n"
        "    if '.tmp-tokens-' in str(path):\n"
        "        raise PermissionError(errno.EPERM, 'Operation not permitted', str(path))\n"
        "    return _orig_chown(path, uid, gid, *a, **kw)\n"
        "os.chown = _deny_tmp_chown\n",
        encoding="utf-8",
    )
    env_with_hook = dict(os.environ)
    prev_py = env_with_hook.get("PYTHONPATH", "")
    env_with_hook["PYTHONPATH"] = f"{hook_dir}:{prev_py}" if prev_py else str(hook_dir)

    res_fb = subprocess.run(
        ["bash", "-c", 'source "$1" && ensure_research_host_tokens "$2"', "_", str(env_tokens_sh), str(env_fallback)],
        env=env_with_hook,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_fb.returncode == 0, res_fb.stderr
    assert "DALTON_RESEARCH_HOST_TOKEN" in res_fb.stdout
    assert "DALTON_RESEARCH_ADMIN_TOKEN" in res_fb.stdout
    st_fb = env_fallback.stat()
    assert stat.S_IMODE(st_fb.st_mode) == 0o600


def _assert_pid_dead(pid: int, *, label: str, timeout_s: float = 1.5) -> None:
    """Assert that process `pid` has actually exited (os.kill(pid, 0) raises ProcessLookupError)."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"{label} (pid={pid}) is still alive after {timeout_s}s")


def _assert_flock_reacquirable(lock_path: Path, *, label: str) -> None:
    """Assert that an exclusive non-blocking flock on `lock_path` can be acquired immediately."""
    import fcntl

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AssertionError(f"{label}: lock {lock_path} is still held by another process") from exc
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_r12_lock_helper_reclaimed_on_handshake_timeout_and_lock_reacquirable(tmp_path: Path) -> None:
    """r12 #1: Python lock helper is tracked directly as $!, killed & reaped on handshake timeout, and lock is immediately re-acquirable."""
    import sys
    import time

    fx = _build_r11_backup_fixture(tmp_path / "r12_lock", project_name="dalton_r12_lock")
    lock_file = tmp_path / "r12_lock" / "reclaim.flock"
    helper_pid_file = tmp_path / "r12_lock" / "helper.pid"
    hold_helper_gate = tmp_path / "r12_lock" / "hold_helper.gate"
    hold_helper_gate.write_text("1", encoding="utf-8")

    # Python shim intercepts ONLY the lock helper invocation, acquires flock, records its real PID, and stalls before writing status
    shim_bin = tmp_path / "r12_lock" / "shim_bin"
    shim_bin.mkdir()
    py_exe = sys.executable
    py_shim = shim_bin / "python3"
    py_shim.write_text(
        f"""#!{py_exe}
import fcntl, os, sys, time
if len(sys.argv) >= 4 and sys.argv[1] == "-c" and "fcntl.flock" in sys.argv[2]:
    try:
        os.setsid()
    except OSError:
        pass
    lock_path = sys.argv[3]
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    with open({str(helper_pid_file)!r}, "w", encoding="utf-8") as pf:
        pf.write(str(os.getpid()))
    deadline = time.monotonic() + 30.0
    while os.path.exists({str(hold_helper_gate)!r}) and time.monotonic() < deadline:
        time.sleep(0.05)
    os.close(fd)
    sys.exit(0)
os.execv({py_exe!r}, [{py_exe!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    py_shim.chmod(0o755)

    out_timeout = tmp_path / "r12_lock" / "out_timeout"
    t0 = time.monotonic()
    try:
        res_to = subprocess.run(
            [
                "bash",
                str(fx["backup_script"]),
                "--work-dir",
                str(fx["work_dir"]),
                "--output-dir",
                str(out_timeout),
            ],
            env={
                **fx["env"],
                "PATH": f"{shim_bin}:{fx['env']['PATH']}",
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_file),
                "DALTON_WIKI_BACKUP_LOCK_WAIT_S": "0.1",
                "DALTON_WIKI_BACKUP_LOCK_HANDSHAKE_TIMEOUT_S": "0.35",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        elapsed = time.monotonic() - t0
        assert res_to.returncode != 0
        assert elapsed < 6.0, f"Handshake timeout took too long: {elapsed:.2f}s"
        assert str(lock_file) in res_to.stderr
        assert helper_pid_file.is_file(), "Stalled Python lock helper never started"
        helper_pid = int(helper_pid_file.read_text(encoding="utf-8").strip())
        # Assert BEFORE finally unlinks hold_helper_gate so shim cannot exit on its own
        _assert_pid_dead(helper_pid, label="stalled Python lock helper")
        _assert_flock_reacquirable(lock_file, label="after handshake timeout")
    finally:
        if hold_helper_gate.exists():
            hold_helper_gate.unlink()
    leftover_stages = [p for p in out_timeout.iterdir() if p.name.startswith(".staging-")] if out_timeout.exists() else []
    assert leftover_stages == [], f"Staging directory not cleaned up: {leftover_stages}"

    # Normal run with the same lock file succeeds, leaves no .wiki-lock-* artifacts, and releases lock cleanly
    out_ok = tmp_path / "r12_lock" / "out_ok"
    res_ok = subprocess.run(
        [
            "bash",
            str(fx["backup_script"]),
            "--work-dir",
            str(fx["work_dir"]),
            "--output-dir",
            str(out_ok),
        ],
        env={
            **fx["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_file),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_ok.returncode == 0, f"normal backup failed:\n{res_ok.stdout}\n{res_ok.stderr}"
    _assert_flock_reacquirable(lock_file, label="after normal release")
    published = [p for p in out_ok.iterdir() if p.is_dir() and p.name.startswith("dalton-backup-")]
    assert len(published) == 1
    leaked_lock_files = [p.name for p in published[0].iterdir() if p.name.startswith(".wiki-lock")]
    assert leaked_lock_files == [], f"Lock handshake files leaked into published backup: {leaked_lock_files}"


def test_r12_process_group_sigkill_kills_sigterm_ignoring_child_after_leader_exits(tmp_path: Path) -> None:
    """r12 #2: Even when pgrp leader exits immediately on SIGTERM, grace period still SIGKILLs SIGTERM-ignoring descendants."""
    import signal
    import time

    fx = _build_r11_backup_fixture(tmp_path / "r12_pgrp", project_name="dalton_r12_pgrp")
    fx["spawn_sigterm_ignoring_child_flag"].write_text("1", encoding="utf-8")
    fx["hang_renew_gate"].write_text("1", encoding="utf-8")
    fx["wait_for_renew_marker"].write_text("0.75", encoding="utf-8")
    lock_file = tmp_path / "r12_pgrp" / "pgrp.flock"

    t0 = time.monotonic()
    try:
        res = subprocess.run(
            [
                "bash",
                str(fx["backup_script"]),
                "--work-dir",
                str(fx["work_dir"]),
                "--output-dir",
                str(tmp_path / "r12_pgrp" / "out"),
            ],
            env={
                **fx["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_file),
                "DALTON_WIKI_PAUSE_TTL_S": "5.0",
                "DALTON_WIKI_PAUSE_HEARTBEAT_S": "0.05",
                "DALTON_WIKI_PAUSE_HEARTBEAT_TIMEOUT_S": "0.2",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        elapsed = time.monotonic() - t0
        assert res.returncode != 0
        assert elapsed < 8.0, f"pgrp cleanup took too long: {elapsed:.2f}s"
        # Confirm grandchild actually reached SIG_IGN barrier before leader accepted SIGTERM
        assert fx["sigterm_ignoring_child_ready_file"].is_file(), "Grandchild never reached SIG_IGN ready barrier"
        leader_pid = int(fx["renew_entered_marker"].read_text(encoding="utf-8").strip())
        child_pid = int(fx["sigterm_ignoring_child_pid_file"].read_text(encoding="utf-8").strip())
        # Assert BOTH leader and SIGTERM-ignoring grandchild are dead BEFORE finally fallback cleanup!
        _assert_pid_dead(leader_pid, label="pgrp leader")
        _assert_pid_dead(child_pid, label="SIGTERM-ignoring descendant in same process group")
    finally:
        if fx["hang_renew_gate"].exists():
            fx["hang_renew_gate"].unlink()
        if fx["sigterm_ignoring_child_pid_file"].exists():
            try:
                child_pid_cleanup = int(fx["sigterm_ignoring_child_pid_file"].read_text(encoding="utf-8").strip())
                os.kill(child_pid_cleanup, signal.SIGKILL)
            except OSError:
                pass


def test_r12_drain_and_resume_outer_timeouts_exit_kill_hung_exec_and_clean_lock_and_stage(
    tmp_path: Path,
) -> None:
    """r12 #3: Outer compose exec timeouts on drain GET and resume POST kill hung processes, release lock, and remove staging dir."""
    import time

    # 3a. Outer compose exec hangs during drain GET
    fx_drain = _build_r11_backup_fixture(tmp_path / "r12_drain_hang", project_name="dalton_r12_dh")
    fx_drain["clock_file"].write_text("1000.0", encoding="utf-8")
    fx_drain["hang_drain_gate"].write_text("1", encoding="utf-8")
    drain_lock = tmp_path / "r12_drain_hang" / "dh.flock"
    drain_out = tmp_path / "r12_drain_hang" / "out"

    t0 = time.monotonic()
    try:
        res_dh = subprocess.run(
            [
                "bash",
                str(fx_drain["backup_script"]),
                "--work-dir",
                str(fx_drain["work_dir"]),
                "--output-dir",
                str(drain_out),
            ],
            env={
                **fx_drain["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(drain_lock),
                "DALTON_WIKI_PAUSE_TTL_S": "300",
                "DALTON_WIKI_PAUSE_HEARTBEAT_S": "60",
                "DALTON_WIKI_EXEC_TIMEOUT_S": "0.2",
                "DALTON_WIKI_DRAIN_TIMEOUT_S": "0.45",
                "DALTON_WIKI_DRAIN_POLL_S": "0.05",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        if fx_drain["hang_drain_gate"].exists():
            fx_drain["hang_drain_gate"].unlink()
    elapsed_dh = time.monotonic() - t0
    assert res_dh.returncode != 0
    assert elapsed_dh < 6.0, f"Hung drain GET did not time out within budget: {elapsed_dh:.2f}s"
    assert "Timed out waiting for in-flight Wiki writes" in res_dh.stderr
    hung_drain_pid = int(fx_drain["drain_entered_marker"].read_text(encoding="utf-8").strip())
    _assert_pid_dead(hung_drain_pid, label="hung drain GET compose exec")
    _assert_flock_reacquirable(drain_lock, label="after drain GET timeout")
    assert not [p for p in drain_out.iterdir() if p.name.startswith(".staging-")]
    drain_events = fx_drain["events_log"].read_text(encoding="utf-8").splitlines()
    assert any(ev.startswith("resume:") for ev in drain_events), f"Expected resume after drain timeout: {drain_events}"
    assert "backup_python" not in drain_events

    # 3b. Outer compose exec hangs during resume POST
    fx_res = _build_r11_backup_fixture(tmp_path / "r12_resume_hang", project_name="dalton_r12_rh")
    fx_res["clock_file"].write_text("2000.0", encoding="utf-8")
    fx_res["hang_resume_gate"].write_text("1", encoding="utf-8")
    res_lock = tmp_path / "r12_resume_hang" / "rh.flock"
    res_out = tmp_path / "r12_resume_hang" / "out"

    t1 = time.monotonic()
    try:
        res_rh = subprocess.run(
            [
                "bash",
                str(fx_res["backup_script"]),
                "--work-dir",
                str(fx_res["work_dir"]),
                "--output-dir",
                str(res_out),
            ],
            env={
                **fx_res["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(res_lock),
                "DALTON_WIKI_PAUSE_TTL_S": "300",
                "DALTON_WIKI_PAUSE_HEARTBEAT_S": "60",
                "DALTON_WIKI_RESUME_TIMEOUT_S": "0.25",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        if fx_res["hang_resume_gate"].exists():
            fx_res["hang_resume_gate"].unlink()
    elapsed_rh = time.monotonic() - t1
    assert res_rh.returncode != 0
    assert elapsed_rh < 6.0, f"Hung resume POST did not time out within budget: {elapsed_rh:.2f}s"
    assert "Failed to resume Wiki writes" in res_rh.stderr
    hung_resume_pid = int(fx_res["resume_entered_marker"].read_text(encoding="utf-8").strip())
    _assert_pid_dead(hung_resume_pid, label="hung resume POST compose exec")
    _assert_flock_reacquirable(res_lock, label="after resume POST timeout")
    assert not [p for p in res_out.iterdir() if p.name.startswith(".staging-")]


def test_r12_ambiguous_pause_timeout_probes_status_and_resumes_owner_lease(tmp_path: Path) -> None:
    """r12 #4: When pause POST times out after taking effect on research-host, backup.sh probes status, resumes its owner lease, kills hung exec, and exits."""
    import json
    import time

    fx = _build_r11_backup_fixture(tmp_path / "r12_pause_ambig", project_name="dalton_r12_pa")
    # Controllable virtual clock guarantees lease cannot expire naturally
    fx["clock_file"].write_text("5000.0", encoding="utf-8")
    fx["hang_pause_after_apply_gate"].write_text("1", encoding="utf-8")
    lock_file = tmp_path / "r12_pause_ambig" / "pa.flock"
    out_dir = tmp_path / "r12_pause_ambig" / "out"

    t0 = time.monotonic()
    try:
        res = subprocess.run(
            [
                "bash",
                str(fx["backup_script"]),
                "--work-dir",
                str(fx["work_dir"]),
                "--output-dir",
                str(out_dir),
            ],
            env={
                **fx["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_file),
                "DALTON_WIKI_PAUSE_TTL_S": "300",
                "DALTON_WIKI_EXEC_TIMEOUT_S": "0.25",
                "DALTON_WIKI_RESUME_TIMEOUT_S": "2.0",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        if fx["hang_pause_after_apply_gate"].exists():
            fx["hang_pause_after_apply_gate"].unlink()
    elapsed = time.monotonic() - t0
    assert res.returncode != 0
    assert elapsed < 6.0, f"Ambiguous pause timeout recovery took too long: {elapsed:.2f}s"
    hung_pause_pid = int(fx["pause_entered_marker"].read_text(encoding="utf-8").strip())
    _assert_pid_dead(hung_pause_pid, label="hung pause POST compose exec")

    events = fx["events_log"].read_text(encoding="utf-8").splitlines()
    assert any(ev.startswith("pause:") for ev in events), events
    assert any(ev.startswith("probe_pause:") for ev in events), events
    assert any(ev.startswith("resume:") for ev in events), events
    assert "backup_python" not in events
    # Verify lease state on research-host is actually UNPAUSED (not left stuck paused for 300s)
    st = json.loads(fx["lease_state"].read_text(encoding="utf-8"))
    assert st["paused"] is False, f"Wiki writes were left stuck paused after ambiguous pause POST timeout: {st}"
    _assert_flock_reacquirable(lock_file, label="after ambiguous pause timeout")
    assert not [p for p in out_dir.iterdir() if p.name.startswith(".staging-")]


def test_r12_command_substitutions_close_fds_8_and_9_before_exec(tmp_path: Path) -> None:
    """r12 #5: Every $(...) while fd 8/9 are open closes them at subshell entry; SIGKILLing backup mid-drain-subshell immediately frees flock & kills heartbeat."""
    import signal
    import time

    fx = _build_r11_backup_fixture(tmp_path / "r12_fdclose", project_name="dalton_r12_fd")
    fx["clock_file"].write_text("6000.0", encoding="utf-8")
    lock_file = tmp_path / "r12_fdclose" / "fd.flock"

    # 5a. Normal run: audit every research-host child call and confirm ZERO inherited fd 8 or fd 9
    res_ok = subprocess.run(
        [
            "bash",
            str(fx["backup_script"]),
            "--work-dir",
            str(fx["work_dir"]),
            "--output-dir",
            str(tmp_path / "r12_fdclose" / "out_ok"),
        ],
        env={
            **fx["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_file),
            "DALTON_WIKI_PAUSE_TTL_S": "300",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "60",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_ok.returncode == 0, f"backup failed:\n{res_ok.stdout}\n{res_ok.stderr}"
    audit_lines = fx["fd_audit_log"].read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) >= 4
    assert all(ln == "fds=[]" for ln in audit_lines), f"Child inherited lock/heartbeat fds: {audit_lines}"

    # 5b. SIGKILL main backup.sh while it is blocked INSIDE the drain GET $(exec 8>&- 9>&-; ...) command substitution.
    # Because the subshell closed fd 8 and fd 9 at entry, killing main backup.sh immediately delivers EOF to BOTH
    # the flock helper and the heartbeat helper even while the drain child is still blocked!
    fx2 = _build_r11_backup_fixture(tmp_path / "r12_fdclose_kill", project_name="dalton_r12_fdk")
    fx2["clock_file"].write_text("7000.0", encoding="utf-8")
    fx2["hang_drain_gate"].write_text("1", encoding="utf-8")
    lock_file2 = tmp_path / "r12_fdclose_kill" / "fdk.flock"

    proc = subprocess.Popen(
        [
            "bash",
            str(fx2["backup_script"]),
            "--work-dir",
            str(fx2["work_dir"]),
            "--output-dir",
            str(tmp_path / "r12_fdclose_kill" / "out_kill"),
        ],
        env={
            **fx2["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_file2),
            "DALTON_WIKI_PAUSE_TTL_S": "300",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "0.1",
            "DALTON_WIKI_EXEC_TIMEOUT_S": "15",
            "DALTON_WIKI_DRAIN_TIMEOUT_S": "20",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    hb_pid: int | None = None
    drain_child_pid: int | None = None
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if fx2["drain_entered_marker"].exists() and fx2["events_log"].exists():
                renews = [
                    ln
                    for ln in fx2["events_log"].read_text(encoding="utf-8").splitlines()
                    if ln.startswith("renew:")
                ]
                if renews:
                    hb_pid = int(renews[-1].split(":")[1])
                    drain_child_pid = int(fx2["drain_entered_marker"].read_text(encoding="utf-8").strip())
                    break
            time.sleep(0.03)
        assert hb_pid is not None and drain_child_pid is not None, "Did not reach blocked drain GET + active heartbeat"

        # SIGKILL main backup.sh while drain child is STILL blocked inside $(exec 8>&- 9>&-; ...)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)

        # Even while drain_child_pid is still blocked on hang_drain_gate, BOTH heartbeat exits AND flock is released!
        _assert_pid_dead(hb_pid, label="heartbeat after SIGKILL during drain $(...)", timeout_s=1.0)
        # Brief wait for flock helper to receive EOF on fd 8 and exit
        flock_deadline = time.monotonic() + 1.5
        reacquired = False
        while time.monotonic() < flock_deadline:
            try:
                _assert_flock_reacquirable(lock_file2, label="probe")
                reacquired = True
                break
            except AssertionError:
                time.sleep(0.03)
        assert reacquired, "Flock was NOT released after SIGKILL during drain $(...) (fd 8 leaked into command substitution!)"
    finally:
        if fx2["hang_drain_gate"].exists():
            fx2["hang_drain_gate"].unlink()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for p in (hb_pid, drain_child_pid):
            if p is not None:
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    pass


def test_r13_run_with_timeout_bounds_stdout_when_leader_exits_and_child_holds_stdout(
    tmp_path: Path,
) -> None:
    """r13 #1: Single deadline covers both pgrp and stdout reading; leader exiting early while descendant holds stdout still terminates within deadline."""
    import signal
    import time

    fx = _build_r11_backup_fixture(tmp_path / "r13_stdout_hold", project_name="dalton_r13_so")
    fx["clock_file"].write_text("8000.0", encoding="utf-8")
    fx["drain_stdout_holder_flag"].write_text("1", encoding="utf-8")
    lock_file = tmp_path / "r13_stdout_hold" / "so.flock"
    out_dir = tmp_path / "r13_stdout_hold" / "out"

    t0 = time.monotonic()
    try:
        res = subprocess.run(
            [
                "bash",
                str(fx["backup_script"]),
                "--work-dir",
                str(fx["work_dir"]),
                "--output-dir",
                str(out_dir),
            ],
            env={
                **fx["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_file),
                "DALTON_WIKI_PAUSE_TTL_S": "300",
                "DALTON_WIKI_PAUSE_HEARTBEAT_S": "60",
                "DALTON_WIKI_EXEC_TIMEOUT_S": "0.25",
                "DALTON_WIKI_DRAIN_TIMEOUT_S": "0.45",
                "DALTON_WIKI_DRAIN_POLL_S": "0.05",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        elapsed = time.monotonic() - t0
        assert res.returncode != 0
        # Must NOT block 30s waiting for the stdout-holding child after leader exited 0!
        assert elapsed < 5.0, f"Command substitution stalled on stdout-holding child: {elapsed:.2f}s"
        assert fx["drain_stdout_holder_pid_file"].is_file(), "Stdout-holding child never started"
        holder_pid = int(fx["drain_stdout_holder_pid_file"].read_text(encoding="utf-8").strip())
        # Assert stdout-holding child is dead BEFORE finally fallback cleanup!
        _assert_pid_dead(holder_pid, label="stdout-holding descendant after leader exit")
        _assert_flock_reacquirable(lock_file, label="after stdout-holder timeout")
        assert not [p for p in out_dir.iterdir() if p.name.startswith(".staging-")]
    finally:
        if fx["drain_stdout_holder_pid_file"].exists():
            try:
                pid_c = int(fx["drain_stdout_holder_pid_file"].read_text(encoding="utf-8").strip())
                os.kill(pid_c, signal.SIGKILL)
            except OSError:
                pass


def test_r13_pause_recovery_retries_until_confirmed_and_resume_retries_in_exit_trap(
    tmp_path: Path,
) -> None:
    """r13 #2: Ambiguous pause POST retries status probes within budget until confirmed & resumed; failed resume POST keeps WIKI_WRITES_PAUSED=1 so EXIT trap retries."""
    import json
    import time

    # 2a. Pause POST times out; probe #1 arrives before pause is visible (returns NONE), probe #2 sees lease and resumes it!
    fx_p = _build_r11_backup_fixture(tmp_path / "r13_pause_retry", project_name="dalton_r13_pr")
    fx_p["clock_file"].write_text("9000.0", encoding="utf-8")
    fx_p["hang_pause_after_apply_gate"].write_text("1", encoding="utf-8")
    fx_p["probe_delay_until_file"].write_text("1", encoding="utf-8")
    lock_p = tmp_path / "r13_pause_retry" / "pr.flock"
    out_p = tmp_path / "r13_pause_retry" / "out"

    try:
        res_p = subprocess.run(
            [
                "bash",
                str(fx_p["backup_script"]),
                "--work-dir",
                str(fx_p["work_dir"]),
                "--output-dir",
                str(out_p),
            ],
            env={
                **fx_p["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_p),
                "DALTON_WIKI_PAUSE_TTL_S": "300",
                "DALTON_WIKI_EXEC_TIMEOUT_S": "0.25",
                "DALTON_WIKI_PAUSE_RECOVERY_TIMEOUT_S": "2.0",
                "DALTON_WIKI_PAUSE_RECOVERY_POLL_S": "0.05",
                "DALTON_WIKI_RESUME_TIMEOUT_S": "2.0",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_p.returncode != 0
        hung_pause_pid = int(fx_p["pause_entered_marker"].read_text(encoding="utf-8").strip())
        _assert_pid_dead(hung_pause_pid, label="hung pause POST exec")
        p_events = fx_p["events_log"].read_text(encoding="utf-8").splitlines()
        probe_events = [ev for ev in p_events if ev.startswith("probe_pause:")]
        assert len(probe_events) >= 2, f"Expected repeated pause probes until confirmed, got: {p_events}"
        assert any(ev.startswith("resume:") for ev in p_events), f"Expected resume after probe #2 confirmed lease: {p_events}"
        st_p = json.loads(fx_p["lease_state"].read_text(encoding="utf-8"))
        assert st_p["paused"] is False, f"Wiki writes left paused after delayed pause probe recovery: {st_p}"
        _assert_flock_reacquirable(lock_p, label="after delayed pause recovery")
        assert not [p for p in out_p.iterdir() if p.name.startswith(".staging-")]
    finally:
        if fx_p["hang_pause_after_apply_gate"].exists():
            fx_p["hang_pause_after_apply_gate"].unlink()

    # 2b. Resume POST #1 times out -> WIKI_WRITES_PAUSED stays 1 -> EXIT trap retries resume_wiki_writes (#2 succeeds & unpauses!)
    fx_r = _build_r11_backup_fixture(tmp_path / "r13_resume_retry", project_name="dalton_r13_rr")
    fx_r["clock_file"].write_text("9500.0", encoding="utf-8")
    fx_r["hang_resume_gate"].write_text("1", encoding="utf-8")
    fx_r["hang_resume_first_only_flag"].write_text("1", encoding="utf-8")
    lock_r = tmp_path / "r13_resume_retry" / "rr.flock"
    out_r = tmp_path / "r13_resume_retry" / "out"

    t0 = time.monotonic()
    try:
        res_r = subprocess.run(
            [
                "bash",
                str(fx_r["backup_script"]),
                "--work-dir",
                str(fx_r["work_dir"]),
                "--output-dir",
                str(out_r),
            ],
            env={
                **fx_r["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_r),
                "DALTON_WIKI_PAUSE_TTL_S": "300",
                "DALTON_WIKI_PAUSE_HEARTBEAT_S": "60",
                "DALTON_WIKI_RESUME_TIMEOUT_S": "0.25",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        elapsed_r = time.monotonic() - t0
        # Main backup failed because resume #1 timed out, but EXIT trap retried resume #2 and unpaused Wiki writes!
        assert res_r.returncode != 0
        assert elapsed_r < 6.0
        hung_res1_pid = int(fx_r["resume_entered_marker"].read_text(encoding="utf-8").strip())
        _assert_pid_dead(hung_res1_pid, label="hung first resume POST exec")
        attempts = int(fx_r["resume_attempts_file"].read_text(encoding="utf-8").strip())
        assert attempts == 2, f"Expected EXIT trap to retry resume_wiki_writes (2 attempts total), got {attempts}"
        st_r = json.loads(fx_r["lease_state"].read_text(encoding="utf-8"))
        assert st_r["paused"] is False, f"EXIT trap retry failed to resume Wiki writes: {st_r}"
        _assert_flock_reacquirable(lock_r, label="after EXIT trap resume retry")
        assert not [p for p in out_r.iterdir() if p.name.startswith(".staging-")]
    finally:
        if fx_r["hang_resume_gate"].exists():
            fx_r["hang_resume_gate"].unlink()


def _extract_run_with_timeout_pg_fn(backup_script: Path) -> str:
    """Extract the `run_with_timeout_pg() { ... }` function definition from backup.sh."""
    lines = backup_script.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("run_with_timeout_pg()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1]) + "\n"


def test_backup_timeout_pg_waits_for_pgrp_child_after_stdout_close_and_bounds_continuous_stdout(
    tmp_path: Path,
) -> None:
    """Backup edge case 1:
    1. When group leader writes stdout and exits 0 while a same-pgrp child closed stdout and keeps running,
       run_with_timeout_pg must NOT return success; it must time out (124) and kill the lingering child.
    2. When a command streams stdout continuously without blocking, inner read loop still enforces the deadline.
    """
    import signal
    import sys
    import time

    backup_script = _REPO_ROOT / "scripts" / "standalone" / "backup.sh"
    fn_src = _extract_run_with_timeout_pg_fn(backup_script)

    # 1a. Direct run_with_timeout_pg: child closes stdout (DEVNULL) and keeps running after leader exits 0
    child_pid_file = tmp_path / "detached_child.pid"
    leader_code = (
        "import os, subprocess, sys, time; "
        f"c = subprocess.Popen([{sys.executable!r}, '-c', "
        "\"import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)\", "
        f"{str(child_pid_file)!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "dl = time.monotonic() + 5.0\n"
        f"while not os.path.exists({str(child_pid_file)!r}) and time.monotonic() < dl: time.sleep(0.01)\n"
        "sys.stdout.write('OK\\n'); sys.stdout.flush(); os._exit(0)\n"
    )
    t0 = time.monotonic()
    try:
        res_det = subprocess.run(
            ["bash", "-c", f"{fn_src}\nrun_with_timeout_pg 0.35 \"$@\"", "_", sys.executable, "-c", leader_code],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        elapsed_det = time.monotonic() - t0
        assert res_det.returncode == 124, (
            f"Expected timeout (124) while same-pgrp child is still running after stdout EOF, "
            f"got rc={res_det.returncode}, stdout={res_det.stdout!r}, stderr={res_det.stderr!r}"
        )
        assert elapsed_det < 4.0, f"Detached child timeout took too long: {elapsed_det:.2f}s"
        assert child_pid_file.is_file(), "Detached child never started"
        detached_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        _assert_pid_dead(detached_pid, label="same-pgrp child that closed stdout")
    finally:
        if child_pid_file.exists():
            try:
                os.kill(int(child_pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
            except OSError:
                pass

    # 1b. End-to-end backup.sh drain poll: same-pgrp child closed stdout & kept running after leader printed "0"
    fx = _build_r11_backup_fixture(tmp_path / "b3_detached", project_name="dalton_b3_det")
    fx["clock_file"].write_text("10000.0", encoding="utf-8")
    fx["drain_detached_child_flag"].write_text("1", encoding="utf-8")
    lock_det = tmp_path / "b3_detached" / "det.flock"
    out_det = tmp_path / "b3_detached" / "out"
    try:
        res_bk = subprocess.run(
            [
                "bash",
                str(fx["backup_script"]),
                "--work-dir",
                str(fx["work_dir"]),
                "--output-dir",
                str(out_det),
            ],
            env={
                **fx["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_det),
                "DALTON_WIKI_PAUSE_TTL_S": "300",
                "DALTON_WIKI_PAUSE_HEARTBEAT_S": "60",
                "DALTON_WIKI_EXEC_TIMEOUT_S": "0.25",
                "DALTON_WIKI_DRAIN_TIMEOUT_S": "0.45",
                "DALTON_WIKI_DRAIN_POLL_S": "0.05",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_bk.returncode != 0, "backup.sh must not treat timed-out drain poll with live child as drained"
        assert fx["drain_detached_child_pid_file"].is_file()
        bk_child_pid = int(fx["drain_detached_child_pid_file"].read_text(encoding="utf-8").strip())
        _assert_pid_dead(bk_child_pid, label="backup drain detached child")
        _assert_flock_reacquirable(lock_det, label="after backup drain detached child timeout")
    finally:
        if fx["drain_detached_child_pid_file"].exists():
            try:
                os.kill(int(fx["drain_detached_child_pid_file"].read_text(encoding="utf-8").strip()), signal.SIGKILL)
            except OSError:
                pass

    # 1c. Continuous stdout flood never stalls inner read loop past the deadline
    flood_pid_file = tmp_path / "flood.pid"
    flood_code = (
        "import os, sys\n"
        f"open({str(flood_pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "buf = b'x' * 65536\n"
        "while True:\n"
        "    try:\n"
        "        os.write(1, buf)\n"
        "    except BrokenPipeError:\n"
        "        break\n"
    )
    t1 = time.monotonic()
    try:
        res_flood = subprocess.run(
            ["bash", "-c", f"{fn_src}\nrun_with_timeout_pg 0.30 \"$@\"", "_", sys.executable, "-c", flood_code],
            capture_output=True,
            text=False,
            timeout=8,
            check=False,
        )
        elapsed_flood = time.monotonic() - t1
        assert res_flood.returncode == 124, f"Expected timeout (124) on continuous stdout flood, got {res_flood.returncode}"
        assert elapsed_flood < 3.0, f"Continuous stdout flood bypassed deadline: {elapsed_flood:.2f}s"
        assert flood_pid_file.is_file()
        _assert_pid_dead(int(flood_pid_file.read_text(encoding="utf-8").strip()), label="continuous stdout flood process")
    finally:
        if flood_pid_file.exists():
            try:
                os.kill(int(flood_pid_file.read_text(encoding="utf-8").strip()), signal.SIGKILL)
            except OSError:
                pass


def test_backup_pause_recovery_non124_retry_headroom_and_resume_allowlist(
    tmp_path: Path,
) -> None:
    """Backup edge case 2:
    1. Non-124 ambiguous pause failure (e.g. curl disconnect exit 52) keeps polling until confirmed or deadline
       instead of stopping on the first NONE.
    2. Single GET probe step timeout leaves retry headroom inside DALTON_WIKI_PAUSE_RECOVERY_TIMEOUT_S even when
       DALTON_WIKI_EXEC_TIMEOUT_S >= DALTON_WIKI_PAUSE_RECOVERY_TIMEOUT_S.
    3. resume_wiki_writes only clears WIKI_WRITES_PAUSED when output is strictly 'OK' or 'EXPIRED'; unexpected
       non-empty output keeps WIKI_WRITES_PAUSED=1 so EXIT trap retries resuming.
    """
    import json
    import time

    # 2a. Non-124 ambiguous pause error (exit 52): probe #1 returns NONE, probe #2 sees active lease -> resumed!
    fx_n = _build_r11_backup_fixture(tmp_path / "b3_non124", project_name="dalton_b3_n124")
    fx_n["clock_file"].write_text("11000.0", encoding="utf-8")
    fx_n["pause_fail_exit_code_file"].write_text("52", encoding="utf-8")
    fx_n["probe_delay_until_file"].write_text("1", encoding="utf-8")
    lock_n = tmp_path / "b3_non124" / "n124.flock"
    out_n = tmp_path / "b3_non124" / "out"

    res_n = subprocess.run(
        [
            "bash",
            str(fx_n["backup_script"]),
            "--work-dir",
            str(fx_n["work_dir"]),
            "--output-dir",
            str(out_n),
        ],
        env={
            **fx_n["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_n),
            "DALTON_WIKI_PAUSE_TTL_S": "300",
            "DALTON_WIKI_EXEC_TIMEOUT_S": "0.5",
            "DALTON_WIKI_PAUSE_RECOVERY_TIMEOUT_S": "1.8",
            "DALTON_WIKI_PAUSE_RECOVERY_POLL_S": "0.05",
            "DALTON_WIKI_RESUME_TIMEOUT_S": "2.0",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_n.returncode != 0
    n_events = fx_n["events_log"].read_text(encoding="utf-8").splitlines()
    n_probes = [ev for ev in n_events if ev.startswith("probe_pause:")]
    assert len(n_probes) >= 2, f"Non-124 ambiguous pause stopped on first NONE instead of polling: {n_events}"
    assert any(ev.startswith("resume:") for ev in n_events), f"Expected resume after probe #2 confirmed lease: {n_events}"
    st_n = json.loads(fx_n["lease_state"].read_text(encoding="utf-8"))
    assert st_n["paused"] is False, f"Wiki writes left paused after non-124 ambiguous pause error: {st_n}"
    _assert_flock_reacquirable(lock_n, label="after non-124 pause recovery")

    # 2b. Single GET probe #1 hangs while EXEC_TIMEOUT_S > RECOVERY_TIMEOUT_S -> must leave retry headroom for probe #2!
    fx_h = _build_r11_backup_fixture(tmp_path / "b3_headroom", project_name="dalton_b3_hr")
    fx_h["clock_file"].write_text("12000.0", encoding="utf-8")
    fx_h["pause_fail_exit_code_file"].write_text("52", encoding="utf-8")
    fx_h["probe_hang_first_only_flag"].write_text("1", encoding="utf-8")
    lock_h = tmp_path / "b3_headroom" / "hr.flock"
    out_h = tmp_path / "b3_headroom" / "out"

    t0 = time.monotonic()
    try:
        res_h = subprocess.run(
            [
                "bash",
                str(fx_h["backup_script"]),
                "--work-dir",
                str(fx_h["work_dir"]),
                "--output-dir",
                str(out_h),
            ],
            env={
                **fx_h["env"],
                "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_h),
                "DALTON_WIKI_PAUSE_TTL_S": "300",
                "DALTON_WIKI_EXEC_TIMEOUT_S": "5.0",
                "DALTON_WIKI_PAUSE_RECOVERY_TIMEOUT_S": "1.2",
                "DALTON_WIKI_PAUSE_RECOVERY_POLL_S": "0.05",
                "DALTON_WIKI_RESUME_TIMEOUT_S": "2.0",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        elapsed_h = time.monotonic() - t0
        assert res_h.returncode != 0
        assert elapsed_h < 6.0, f"Probe headroom test took too long: {elapsed_h:.2f}s"
        assert fx_h["probe_hang_entered_marker"].is_file()
        _assert_pid_dead(int(fx_h["probe_hang_entered_marker"].read_text(encoding="utf-8").strip()), label="hung probe #1")
        h_events = fx_h["events_log"].read_text(encoding="utf-8").splitlines()
        assert any(ev.startswith("resume:") for ev in h_events), (
            f"Single hung GET consumed entire recovery budget without retry headroom: {h_events}"
        )
        st_h = json.loads(fx_h["lease_state"].read_text(encoding="utf-8"))
        assert st_h["paused"] is False, f"Wiki writes left paused when probe #1 hung: {st_h}"
        _assert_flock_reacquirable(lock_h, label="after probe headroom recovery")
    finally:
        if fx_h["probe_hang_first_only_flag"].exists():
            fx_h["probe_hang_first_only_flag"].unlink()

    # 2c. Resume response with unexpected non-allowlisted stdout ("UNEXPECTED_BANNER") does NOT clear WIKI_WRITES_PAUSED;
    #     EXIT trap retries resume_wiki_writes (#2 returns OK and unpauses)!
    fx_a = _build_r11_backup_fixture(tmp_path / "b3_allowlist", project_name="dalton_b3_al")
    fx_a["clock_file"].write_text("13000.0", encoding="utf-8")
    fx_a["resume_bad_output_first_only_file"].write_text("UNEXPECTED_BANNER", encoding="utf-8")
    lock_a = tmp_path / "b3_allowlist" / "al.flock"
    out_a = tmp_path / "b3_allowlist" / "out"

    res_a = subprocess.run(
        [
            "bash",
            str(fx_a["backup_script"]),
            "--work-dir",
            str(fx_a["work_dir"]),
            "--output-dir",
            str(out_a),
        ],
        env={
            **fx_a["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_a),
            "DALTON_WIKI_PAUSE_TTL_S": "300",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "60",
            "DALTON_WIKI_RESUME_TIMEOUT_S": "2.0",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_a.returncode != 0, "backup.sh must fail when resume returns unexpected non-OK/EXPIRED output"
    attempts_a = int(fx_a["resume_attempts_file"].read_text(encoding="utf-8").strip())
    assert attempts_a == 2, f"Expected EXIT trap retry after unexpected resume output (2 attempts), got {attempts_a}"
    st_a = json.loads(fx_a["lease_state"].read_text(encoding="utf-8"))
    assert st_a["paused"] is False, f"EXIT trap retry did not unpause after unexpected resume output: {st_a}"
    _assert_flock_reacquirable(lock_a, label="after resume allowlist retry")


def test_backup_sigterm_during_pending_pause_probes_and_resumes_writes(
    tmp_path: Path,
) -> None:
    """R2 item 8a: If SIGTERM arrives after research-host paused writes but before backup.sh receives
    the POST lease response, EXIT cleanup probes by owner within deadline and resumes Wiki writes."""
    import json
    import signal
    import time

    fx = _build_r11_backup_fixture(tmp_path / "r2_term_pause", project_name="dalton_r2_tp")
    fx["clock_file"].write_text("14000.0", encoding="utf-8")
    fx["hang_pause_after_apply_gate"].write_text("1", encoding="utf-8")
    lock_f = tmp_path / "r2_term_pause" / "tp.flock"
    out_dir = tmp_path / "r2_term_pause" / "out"

    proc = subprocess.Popen(
        [
            "bash",
            str(fx["backup_script"]),
            "--work-dir",
            str(fx["work_dir"]),
            "--output-dir",
            str(out_dir),
        ],
        env={
            **fx["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_f),
            "DALTON_WIKI_PAUSE_TTL_S": "300",
            "DALTON_WIKI_EXEC_TIMEOUT_S": "15.0",
            "DALTON_WIKI_PAUSE_RECOVERY_TIMEOUT_S": "2.0",
            "DALTON_WIKI_PAUSE_RECOVERY_POLL_S": "0.05",
            "DALTON_WIKI_RESUME_TIMEOUT_S": "2.0",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait until fake research-host has actually applied paused=True (POST is still hanging)
        wait_deadline = time.monotonic() + 10.0
        while not fx["pause_entered_marker"].is_file() and time.monotonic() < wait_deadline:
            assert proc.poll() is None, f"backup.sh exited prematurely: {proc.communicate()}"
            time.sleep(0.05)
        assert fx["pause_entered_marker"].is_file(), "Pause POST never entered fake research-host"
        st_mid = json.loads(fx["lease_state"].read_text(encoding="utf-8"))
        assert st_mid["paused"] is True, f"Expected server paused before SIGTERM: {st_mid}"

        t_term = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=10.0)
        elapsed_after_term = time.monotonic() - t_term
        assert proc.returncode != 0, f"Expected non-zero exit on SIGTERM, stdout={stdout} stderr={stderr}"
        assert elapsed_after_term < 8.0, f"SIGTERM cleanup took too long ({elapsed_after_term:.2f}s)"

        events = fx["events_log"].read_text(encoding="utf-8").splitlines()
        assert any(ev.startswith("probe_pause:") for ev in events), f"Expected owner probe on EXIT cleanup: {events}"
        assert any(ev.startswith("resume:") for ev in events), f"Expected resume after owner probe: {events}"
        st_after = json.loads(fx["lease_state"].read_text(encoding="utf-8"))
        assert st_after["paused"] is False, f"Wiki writes left paused after SIGTERM during pending POST: {st_after}"
        _assert_flock_reacquirable(lock_f, label="after SIGTERM during pending pause POST")
    finally:
        if fx["hang_pause_after_apply_gate"].exists():
            fx["hang_pause_after_apply_gate"].unlink()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_backup_publish_lock_pid_write_failure_and_empty_pid_recovery(
    tmp_path: Path,
) -> None:
    """R2 item 8b:
    1. When writing `${LOCK_DIR}/pid` fails (e.g. ENOSPC leaving an empty/unwritten file),
       `acquire_publish_lock` immediately revokes the lock directory and exits non-zero.
    2. When an orphan `${LOCK_DIR}` exists with a 0-byte empty `pid` file, a subsequent
       `backup.sh` run treats the empty PID file as missing, removes the stale lock after
       the grace period, and completes the backup cleanly.
    """
    # 1. Simulate PID write failure (e.g. ENOSPC leaving an empty pid file and returning non-zero)
    lock_test_dir = tmp_path / "pid_fail_out"
    lock_test_dir.mkdir(parents=True)
    backup_src = (_REPO_ROOT / "scripts" / "standalone" / "backup.sh").read_text(encoding="utf-8")
    # Extract pid_is_alive, acquire_publish_lock, release_publish_lock directly from backup.sh
    fn_start = backup_src.index("pid_is_alive() {")
    fn_end = backup_src.index("cleanup() {")
    lock_funcs = backup_src[fn_start:fn_end]
    harness = f"""
set -euo pipefail
err() {{ echo "ERR: $*" >&2; }}
warn() {{ echo "WARN: $*" >&2; }}
LOCK_DIR={str(lock_test_dir / '.dalton-backup.lock')!r}
LOCK_HELD=0
{lock_funcs}
printf() {{ : > "${{LOCK_DIR}}/pid"; return 1; }}
acquire_publish_lock
"""
    res_fail = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_fail.returncode != 0, "acquire_publish_lock must fail when PID write fails"
    assert not (lock_test_dir / ".dalton-backup.lock").exists(), (
        "Lock directory must be revoked when writing PID file fails"
    )

    # 2. End-to-end backup.sh recovers from an orphan lock containing an empty 0-byte PID file
    fx = _build_r11_backup_fixture(tmp_path / "r2_empty_pid", project_name="dalton_r2_ep")
    fx["clock_file"].write_text("15000.0", encoding="utf-8")
    lock_f = tmp_path / "r2_empty_pid" / "ep.flock"
    out_dir = tmp_path / "r2_empty_pid" / "out"
    stale_lock = out_dir / ".dalton-backup.lock"
    stale_lock.mkdir(parents=True, exist_ok=True)
    (stale_lock / "pid").write_text("", encoding="utf-8")  # 0-byte empty PID file

    res_ok = subprocess.run(
        [
            "bash",
            str(fx["backup_script"]),
            "--work-dir",
            str(fx["work_dir"]),
            "--output-dir",
            str(out_dir),
        ],
        env={
            **fx["env"],
            "DALTON_WIKI_BACKUP_LOCK_FILE": str(lock_f),
            "DALTON_WIKI_PAUSE_TTL_S": "300",
            "DALTON_WIKI_PAUSE_HEARTBEAT_S": "60",
            "DALTON_PUBLISH_LOCK_MISSING_PID_GRACE_S": "1",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res_ok.returncode == 0, f"backup.sh failed to recover from empty PID file: {res_ok.stderr}"
    assert "empty/invalid PID file" in res_ok.stderr
    assert not stale_lock.exists(), "Publish lock should be released after successful backup"
    backups = [p for p in out_dir.iterdir() if p.is_dir() and p.name.startswith("dalton-backup-")]
    assert len(backups) == 1, f"Expected 1 published backup after empty-PID lock recovery, got {backups}"


