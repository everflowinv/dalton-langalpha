"""Unit tests for scripts/standalone/upgrade.sh (Round 5 Decision #8 & Round 7 Decisions #5-#6).

Uses fake ``docker`` and ``curl`` binaries on PATH (same pattern as
``test_deploy_main_revert.py``) so tests run hermetically without a Docker daemon.
"""

from __future__ import annotations

import os
import shutil
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
        assert "/health" in trace_txt and "/agent/settings" in trace_txt
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
    #    the curl fallback branch and the node branch without opening network sockets
    #    (unit tests run under pytest-socket):
    #    - missing/wrong token -> fail
    #    - /health {"ok":true,"ready":false} -> fail (still starting)
    #    - /health {"ok":true,"ready":true} -> pass
    #    - /health {"ok":true} (no ready field) + /agent/settings 503 gateway_starting -> fail
    #    - /health {"ok":true} (no ready field) + /agent/settings 200 -> pass
    script_src = _UPGRADE_SCRIPT.read_text(encoding="utf-8")
    m_probe = _re.search(
        r"exec -T -e DALTON_HEALTH_TOKEN channel-gateway sh -c \\\n\s+'([^']+)'",
        script_src,
    )
    assert m_probe is not None, "Expected channel-gateway sh -c readiness probe in upgrade.sh"
    probe_snippet = m_probe.group(1)

    base_url = "http://127.0.0.1:8962"
    base_probe_env = {k: v for k, v in os.environ.items() if k != "DALTON_HEALTH_TOKEN"}

    # 3a. curl fallback branch (PATH contains fake curl + /usr/bin for tr, NO node)
    curl_bin = tmp_path / "probe-curl-bin"
    curl_bin.mkdir()
    tr_bin = Path("/usr/bin/tr")
    if tr_bin.exists():
        (curl_bin / "tr").symlink_to(tr_bin)
    fake_curl = curl_bin / "curl"
    fake_curl.write_text(
        f"""#!/bin/bash
set -euo pipefail
hdr=""
target=""
write_code=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -H)
      hdr="${{2:-}}"
      shift 2
      ;;
    -o)
      shift 2
      ;;
    -w)
      write_code=1
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
  if [[ "$write_code" -eq 1 ]]; then echo "401"; exit 0; fi
  exit 22
fi
if [[ "$target" == "{base_url}/health" ]]; then
  printf '%s' "${{FAKE_HEALTH_BODY:-}}"
  exit "${{FAKE_HEALTH_RC:-0}}"
fi
if [[ "$target" == "{base_url}/agent/settings" ]]; then
  if [[ "$write_code" -eq 1 ]]; then
    echo "${{FAKE_SETTINGS_CODE:-200}}"
    exit 0
  fi
  exit 0
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
        ("health-ready-true", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok": true, "ready": true}'}, True),
        ("health-no-ready-settings-503", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok": true}', "FAKE_SETTINGS_CODE": "503"}, False),
        ("health-no-ready-settings-200", {"DALTON_HEALTH_TOKEN": secret_tok, "FAKE_HEALTH_BODY": '{"ok": true}', "FAKE_SETTINGS_CODE": "200"}, True),
    ):
        r_c = subprocess.run(
            ["/bin/sh", "-c", probe_snippet, "_", base_url],
            env={**curl_env, **extra_env},
            text=True,
            capture_output=True,
            check=False,
        )
        assert (r_c.returncode == 0) is expect_ok, f"curl branch {label}: rc={r_c.returncode} err={r_c.stderr}"

    # 3b. node branch (PATH has fake node; verifies JS checks /health ready + /agent/settings 200 fallback)
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
if [[ "$js" != *"/health"* || "$js" != *"ready"* || "$js" != *"/agent/settings"* ]]; then exit 5; fi
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



