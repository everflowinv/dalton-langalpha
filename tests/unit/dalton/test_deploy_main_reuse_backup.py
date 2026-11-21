"""--reuse-backup-minutes: a recent ok dump is reused only when no migration is being deployed."""

from __future__ import annotations

import subprocess
from pathlib import Path

_DEPLOY_SCRIPT = Path(__file__).resolve().parents[3] / "scripts/dalton/ops/prod/deploy_main.sh"


def _run(body: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
{body}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False, cwd=cwd)


def _epoch(ts: str) -> int:
    out = subprocess.run(
        ["bash", "-c", f"date -j -f '%Y-%m-%d %H:%M:%S' '{ts}' +%s 2>/dev/null || date -d '{ts}' +%s"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def test_parse_accepts_positive_minutes_and_rejects_others() -> None:
    ok = _run('parse_deploy_args --reuse-backup-minutes 90 && echo "min=$REUSE_BACKUP_MIN"')
    assert ok.returncode == 0 and "min=90" in ok.stdout
    for bad in ("", "0", "-5", "abc", "1.5"):
        res = _run(f'parse_deploy_args --reuse-backup-minutes "{bad}"')
        assert res.returncode == 2, bad


def test_recent_ok_backup_reuses_only_fresh_ok_lines(tmp_path: Path) -> None:
    log = tmp_path / "backup.log"
    line = "2026-11-21 10:41:49 ok  16G /backups/dalton-pg-20261121-102259.dump"
    log.write_text("2026-11-21 06:26:59 ok  16G /backups/old.dump\n" + line + "\n")
    now = _epoch("2026-11-21 11:00:00")
    fresh = _run(f'recent_ok_backup "{log}" 90 {now}')
    assert fresh.returncode == 0 and fresh.stdout.strip() == line
    stale = _run(f'recent_ok_backup "{log}" 10 {now}')
    assert stale.returncode == 1 and stale.stdout == ""
    log.write_text("2026-11-21 10:55:00 FAILED pg_dump exited 1\n")
    failed = _run(f'recent_ok_backup "{log}" 90 {now}')
    assert failed.returncode == 1
    future = _run(f'recent_ok_backup "{log}" 90 {_epoch("2026-11-21 10:00:00")}')
    assert future.returncode == 1
    missing = _run(f'recent_ok_backup "{tmp_path / "none.log"}" 90 {now}')
    assert missing.returncode == 1


def test_no_pending_migrations_detects_migration_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "Test"], check=True)
    (repo / "src/dalton/migrations/versions").mkdir(parents=True)
    (repo / "src/dalton/migrations/versions/d041_x.py").write_text("")
    (repo / "app.py").write_text("v1")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "v1"], check=True)
    subprocess.run([*git, "branch", "code-only"], check=True)
    subprocess.run([*git, "checkout", "-q", "code-only"], check=True)
    (repo / "app.py").write_text("v2")
    subprocess.run([*git, "commit", "-qam", "code"], check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "with-migration"], check=True)
    (repo / "src/dalton/migrations/versions/d042_y.py").write_text("")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "mig"], check=True)
    subprocess.run([*git, "checkout", "-q", "-"], check=True)
    subprocess.run([*git, "checkout", "-q", "HEAD~1"], check=True)
    assert _run("no_pending_migrations code-only", cwd=repo).returncode == 0
    assert _run("no_pending_migrations with-migration", cwd=repo).returncode == 1
    assert _run("no_pending_migrations no-such-ref", cwd=repo).returncode == 1
