"""Tests for pinned deploy target, DB-based migration detection, and --reuse-backup-minutes."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_SCRIPT = _REPO_ROOT / "scripts/dalton/ops/prod/deploy_main.sh"


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


def _setup_deploy_env(tmp_path: Path) -> dict[str, Path | str]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    (seed / "src/dalton/migrations/versions").mkdir(parents=True)
    (seed / "migrations/versions").mkdir(parents=True)
    (seed / "src/dalton/migrations/versions/d040_base.py").write_text(
        "revision = 'd040'\ndown_revision = None\n"
    )
    (seed / "migrations/versions/146_base.py").write_text(
        "revision = '146'\ndown_revision = None\n"
    )
    (seed / "scripts/dalton").mkdir(parents=True)
    (seed / "web").mkdir(parents=True)
    (seed / "web/.keep").write_text("")
    (seed / "app.py").write_text("v1\n")
    (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
        "#!/bin/bash\necho 'OK: Production bundle'\nexit 0\n"
    )
    (seed / "scripts/dalton/check_public_edge.sh").write_text("#!/bin/bash\necho 'edge ok'\nexit 0\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v1"], check=True)
    subprocess.run(["git", "-C", str(seed), "branch", "-M", "dalton/main"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "dalton/main"], check=True)
    v1 = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "-b", "dalton/main", str(origin), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    backup_log = tmp_path / "pg-backup.log"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    backup_log.write_text(f"{now_str} ok  16G /backups/recent.dump\n")
    launchctl_trace = tmp_path / "launchctl.trace"
    wait_idle = tmp_path / "wait_idle.sh"
    wait_idle.write_text("#!/bin/bash\nexit 0\n")
    wait_idle.chmod(0o755)

    (fake_bin / "launchctl").write_text(
        f"""#!/bin/bash
echo "$*" >> "{launchctl_trace}"
if [ "$1" = "kickstart" ]; then
  echo "{now_str} ok  16G /backups/fresh.dump" >> "{backup_log}"
fi
exit 0
"""
    )
    (fake_bin / "docker").write_text(
        """#!/bin/bash
case "$1" in
  exec)
    if echo "$*" | grep -q "conversation_threads"; then
      echo "0"
      exit 0
    fi
    ;;
esac
exit 0
"""
    )
    (fake_bin / "curl").write_text("#!/bin/bash\necho 200\nexit 0\n")
    (fake_bin / "uv").write_text("#!/bin/bash\nexit 0\n")
    (fake_bin / "pnpm").write_text("#!/bin/bash\nexit 0\n")
    for name in ("launchctl", "docker", "curl", "uv", "pnpm"):
        (fake_bin / name).chmod(0o755)

    return {
        "origin": origin,
        "seed": seed,
        "repo": repo,
        "v1": v1,
        "fake_bin": fake_bin,
        "backup_log": backup_log,
        "launchctl_trace": launchctl_trace,
        "wait_idle": wait_idle,
    }


def _run_deploy(
    env_ctx: dict[str, Path | str],
    tmp_path: Path,
    *,
    args: str = "",
    dalton_fn: str = "printf 'd040'",
    upstream_fn: str = "printf '146'",
    extra_setup: str = "",
) -> subprocess.CompletedProcess[str]:
    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
WAIT_IDLE_SCRIPT="{env_ctx['wait_idle']}"
get_dalton_alembic_version() {{ {dalton_fn}; }}
get_upstream_alembic_version() {{ {upstream_fn}; }}
{extra_setup}
run_deploy_main {args}
"""
    return subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{env_ctx['fake_bin']}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(env_ctx["repo"]),
            "DALTON_PG_BACKUP_LOG": str(env_ctx["backup_log"]),
            "DALTON_ALEMBIC_QUERY_RETRY_DELAY_S": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )


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


def test_no_pending_migrations_compares_db_revisions_against_target_heads(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "Test"], check=True)
    (repo / "src/dalton/migrations/versions").mkdir(parents=True)
    (repo / "migrations/versions").mkdir(parents=True)
    (repo / "src/dalton/migrations/versions/d040_base.py").write_text(
        "revision = 'd040'\ndown_revision = None\n"
    )
    (repo / "migrations/versions/146_base.py").write_text(
        "revision = '146'\ndown_revision = None\n"
    )
    (repo / "app.py").write_text("v1")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "v1"], check=True)

    subprocess.run([*git, "checkout", "-q", "-b", "code-only"], check=True)
    (repo / "app.py").write_text("v2")
    subprocess.run([*git, "commit", "-qam", "code"], check=True)

    subprocess.run([*git, "checkout", "-q", "-b", "with-dalton-mig"], check=True)
    (repo / "src/dalton/migrations/versions/d042_y.py").write_text(
        "revision = 'd042'\ndown_revision = 'd040'\n"
    )
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "dalton mig"], check=True)

    subprocess.run([*git, "checkout", "-q", "-b", "with-upstream-mig", "code-only"], check=True)
    (repo / "migrations/versions/147_y.py").write_text(
        "revision = '147'\ndown_revision = '146'\n"
    )
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "upstream mig"], check=True)
    subprocess.run([*git, "checkout", "-q", "code-only~1"], check=True)

    assert _run('PRE_FF_DALTON_REV=d040 PRE_FF_UPSTREAM_REV=146 no_pending_migrations code-only', cwd=repo).returncode == 0
    assert _run('PRE_FF_DALTON_REV=d040 PRE_FF_UPSTREAM_REV=146 no_pending_migrations with-dalton-mig', cwd=repo).returncode == 1
    assert _run('PRE_FF_DALTON_REV=d040 PRE_FF_UPSTREAM_REV=146 no_pending_migrations with-upstream-mig', cwd=repo).returncode == 1
    unreadable = _run('PRE_FF_DALTON_REV="" PRE_FF_UPSTREAM_REV=146 no_pending_migrations code-only', cwd=repo)
    assert unreadable.returncode == 1
    assert "unreadable" in unreadable.stderr
    assert _run('PRE_FF_DALTON_REV=d040 PRE_FF_UPSTREAM_REV=146 no_pending_migrations no-such-ref', cwd=repo).returncode == 1


def test_target_migration_heads_resolves_unique_heads_and_rejects_multiple_heads(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
    subprocess.run([*git, "config", "user.name", "Test"], check=True)
    (repo / "src/dalton/migrations/versions").mkdir(parents=True)
    (repo / "migrations/versions").mkdir(parents=True)
    (repo / "src/dalton/migrations/versions/d001_init.py").write_text(
        '"""Docstring mentioning revision = "fake"."""\n'
        'revision: str = "d001"\n'
        "down_revision = None\n"
    )
    (repo / "migrations/versions/145_init.py").write_text(
        "revision = '145'\ndown_revision = None\n"
    )
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    base_sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()

    # Non-linear Dalton chain (d001 -> d003 -> d002) and merged upstream chain (145 -> {146a, 146b} -> 147)
    subprocess.run([*git, "checkout", "-q", "-b", "target-ok"], check=True)
    (repo / "src/dalton/migrations/versions/d003_mid.py").write_text(
        'revision = "d003"\ndown_revision: str = "d001"\n'
    )
    (repo / "src/dalton/migrations/versions/d002_head.py").write_text(
        'revision = "d002"\ndown_revision = "d003"\n'
    )
    (repo / "migrations/versions/146a_left.py").write_text(
        "revision = '146a'\ndown_revision = '145'\n"
    )
    (repo / "migrations/versions/146b_right.py").write_text(
        "revision = '146b'\ndown_revision = '145'\n"
    )
    (repo / "migrations/versions/147_merge.py").write_text(
        "from typing import Sequence, Union\n"
        "revision: str = '147'\n"
        "down_revision: Union[str, Sequence[str], None] = ('146a', '146b')\n"
    )
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "ok heads"], check=True)
    ok_sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()

    # Two heads in Dalton chain
    subprocess.run([*git, "checkout", "-q", "-b", "two-dalton-heads", base_sha], check=True)
    (repo / "src/dalton/migrations/versions/d002a_left.py").write_text(
        "revision = 'd002a'\ndown_revision = 'd001'\n"
    )
    (repo / "src/dalton/migrations/versions/d002b_right.py").write_text(
        "revision = 'd002b'\ndown_revision = 'd001'\n"
    )
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "two dalton heads"], check=True)
    bad_dalton_sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()

    # Two heads in upstream chain
    subprocess.run([*git, "checkout", "-q", "-b", "two-upstream-heads", base_sha], check=True)
    (repo / "migrations/versions/146a_left.py").write_text(
        "revision = '146a'\ndown_revision = '145'\n"
    )
    (repo / "migrations/versions/146b_right.py").write_text(
        "revision = '146b'\ndown_revision = '145'\n"
    )
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "two upstream heads"], check=True)
    bad_upstream_sha = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()

    # Working tree stays on base_sha; target_migration_heads reads directly from git history
    subprocess.run([*git, "checkout", "-q", base_sha], check=True)

    res_ok = _run(f'target_migration_heads "{ok_sha}"', cwd=repo)
    assert res_ok.returncode == 0, res_ok.stderr
    assert res_ok.stdout.strip() == "d002 147"

    res_bad_d = _run(f'target_migration_heads "{bad_dalton_sha}"', cwd=repo)
    assert res_bad_d.returncode != 0
    assert "expected 1 head, found 2" in res_bad_d.stderr
    assert "d002a" in res_bad_d.stderr and "d002b" in res_bad_d.stderr

    res_bad_u = _run(f'target_migration_heads "{bad_upstream_sha}"', cwd=repo)
    assert res_bad_u.returncode != 0
    assert "expected 1 head, found 2" in res_bad_u.stderr
    assert "146a" in res_bad_u.stderr and "146b" in res_bad_u.stderr

    # Also validate against the actual repository HEAD
    real_heads = _run("target_migration_heads HEAD", cwd=_REPO_ROOT)
    assert real_heads.returncode == 0, real_heads.stderr
    dalton_head, upstream_head = real_heads.stdout.strip().split()
    assert dalton_head.startswith("d")
    assert upstream_head.isdigit()


def test_run_deploy_main_fetches_once_and_deploys_pinned_target_even_if_remote_advances(
    tmp_path: Path,
) -> None:
    ctx = _setup_deploy_env(tmp_path)
    seed = Path(ctx["seed"])
    repo = Path(ctx["repo"])
    fake_bin = Path(ctx["fake_bin"])
    launchctl_trace = Path(ctx["launchctl_trace"])

    # Push v2 (code-only, same migration heads d040/146)
    (seed / "app.py").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "commit", "-qam", "v2 code only"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    v2 = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()

    # Wrap git to count fetches and push a late v3 migration commit right after the first fetch
    real_git = subprocess.check_output(["which", "git"], text=True).strip()
    fetch_log = tmp_path / "fetch.log"
    pushed_marker = tmp_path / "pushed_v3"
    (fake_bin / "git").write_text(
        f"""#!/bin/bash
if [ "${{1:-}}" = "fetch" ]; then
  echo "fetch:$*" >> "{fetch_log}"
  "{real_git}" "$@"
  rc=$?
  if [ "$rc" -eq 0 ] && [ ! -f "{pushed_marker}" ]; then
    touch "{pushed_marker}"
    cat > "{seed}/src/dalton/migrations/versions/d042_late.py" <<'EOF'
revision = 'd042'
down_revision = 'd040'
EOF
    "{real_git}" -C "{seed}" add src/dalton/migrations/versions/d042_late.py
    "{real_git}" -C "{seed}" commit -q -m "v3 late migration"
    "{real_git}" -C "{seed}" push -q origin dalton/main
  fi
  exit "$rc"
fi
exec "{real_git}" "$@"
"""
    )
    (fake_bin / "git").chmod(0o755)

    res = _run_deploy(ctx, tmp_path, args="--reuse-backup-minutes 60")
    assert res.returncode == 0, res.stdout + res.stderr
    assert fetch_log.read_text().splitlines() == ["fetch:fetch -q origin"]
    assert f"deploy target: {v2}" in res.stdout
    assert "backup: reusing" in res.stdout
    assert not launchctl_trace.exists()
    assert "migrating_deploy=0" in res.stdout
    deployed_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    remote_head = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()
    assert deployed_head == v2
    assert remote_head != v2


def test_run_deploy_main_retry_when_code_already_at_target_but_db_behind_requires_backup(
    tmp_path: Path,
) -> None:
    ctx = _setup_deploy_env(tmp_path)
    seed = Path(ctx["seed"])
    repo = Path(ctx["repo"])
    launchctl_trace = Path(ctx["launchctl_trace"])

    # Push v2 with a new Dalton migration and fast-forward repo already to v2 (simulating retry)
    (seed / "src/dalton/migrations/versions/d042_retry.py").write_text(
        "revision = 'd042'\ndown_revision = 'd040'\n"
    )
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v2 migration"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    v2 = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()

    subprocess.run(["git", "-C", str(repo), "pull", "-q", "--ff-only"], check=True)
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == v2

    # DB is still at old d040/146
    res = _run_deploy(
        ctx,
        tmp_path,
        args="--reuse-backup-minutes 60",
        dalton_fn="printf 'd040'",
        upstream_fn="printf '146'",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "backup: cannot reuse" in res.stdout
    assert launchctl_trace.exists()
    assert "kickstart" in launchctl_trace.read_text()
    assert "migrating_deploy=1" in res.stdout


def test_run_deploy_main_reuses_backup_when_db_matches_target_heads(tmp_path: Path) -> None:
    ctx = _setup_deploy_env(tmp_path)
    seed = Path(ctx["seed"])
    repo = Path(ctx["repo"])
    launchctl_trace = Path(ctx["launchctl_trace"])

    (seed / "app.py").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "commit", "-qam", "v2 code only"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    v2 = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()

    res = _run_deploy(
        ctx,
        tmp_path,
        args="--reuse-backup-minutes 60",
        dalton_fn="printf 'd040'",
        upstream_fn="printf '146'",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "backup: reusing" in res.stdout
    assert not launchctl_trace.exists()
    assert "migrating_deploy=0" in res.stdout
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == v2


def test_run_deploy_main_treats_unreadable_db_version_as_migrating_and_forces_backup(
    tmp_path: Path,
) -> None:
    ctx = _setup_deploy_env(tmp_path)
    seed = Path(ctx["seed"])
    launchctl_trace = Path(ctx["launchctl_trace"])

    (seed / "app.py").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "commit", "-qam", "v2 code only"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)

    res = _run_deploy(
        ctx,
        tmp_path,
        args="--reuse-backup-minutes 60",
        dalton_fn="return 1",
        upstream_fn="printf '146'",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "DB version unreadable" in res.stdout
    assert "backup: cannot reuse" in res.stdout
    assert launchctl_trace.exists()
    assert "kickstart" in launchctl_trace.read_text()
    assert "migrating_deploy=1" in res.stdout


def test_run_deploy_main_aborts_before_deploy_when_target_has_multiple_heads(
    tmp_path: Path,
) -> None:
    ctx = _setup_deploy_env(tmp_path)
    seed = Path(ctx["seed"])
    repo = Path(ctx["repo"])
    v1 = str(ctx["v1"])
    launchctl_trace = Path(ctx["launchctl_trace"])

    (seed / "src/dalton/migrations/versions/d042a_left.py").write_text(
        "revision = 'd042a'\ndown_revision = 'd040'\n"
    )
    (seed / "src/dalton/migrations/versions/d042b_right.py").write_text(
        "revision = 'd042b'\ndown_revision = 'd040'\n"
    )
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "two dalton heads"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)

    # Even without --reuse-backup-minutes, deploy must abort before backup and before ff
    res = _run_deploy(ctx, tmp_path, args="")
    assert res.returncode != 0
    assert "expected 1 head, found 2" in res.stderr
    assert not launchctl_trace.exists()
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == v1
    assert os.path.exists(repo / "app.py")
