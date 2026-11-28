"""Unit tests for scripts/dalton/ops/prod/deploy_main.sh."""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_SCRIPT = _REPO_ROOT / "scripts/dalton/ops/prod/deploy_main.sh"
_WATCHDOG_SCRIPT = _REPO_ROOT / "scripts/dalton/ops/prod/watchdog.sh"
_OPS_LOCK_SCRIPT = _REPO_ROOT / "scripts/dalton/ops/prod/_ops_lock.sh"


def _init_temp_repo(tmp_path: Path, *, with_migration_change: bool = True) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "src/dalton/migrations/versions").mkdir(parents=True)
    (repo / "migrations/versions").mkdir(parents=True)
    (repo / "src/dalton/migrations/versions/d040_baseline.py").write_text(
        "revision = 'd040'\ndown_revision = None\n"
    )
    (repo / "migrations/versions/146_baseline.py").write_text(
        "revision = '146'\ndown_revision = None\n"
    )
    (repo / "web").mkdir()
    (repo / "scripts/dalton").mkdir(parents=True)
    (repo / "file.txt").write_text("v1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1"], check=True)
    pre_ff = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    if with_migration_change:
        (repo / "src/dalton/migrations/versions/d041_next.py").write_text(
            "revision = 'd041'\ndown_revision = 'd040'\n"
        )
        (repo / "migrations/versions/147_next.py").write_text(
            "revision = '147'\ndown_revision = '146'\n"
        )
    (repo / "file.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return repo, pre_ff, head


def _make_fake_docker(
    tmp_path: Path,
    *,
    dalton_out: str = "d040\n",
    upstream_out: str = "146\n",
    dalton_rc: int = 0,
    upstream_rc: int = 0,
    fail_dalton_until_attempt: int = 0,
    sleep_dalton_s: float = 0,
) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    trace = tmp_path / "docker.trace"
    dalton_file = tmp_path / "dalton_out"
    upstream_file = tmp_path / "upstream_out"
    attempt_file = tmp_path / "dalton_attempt"
    dalton_file.write_text(dalton_out)
    upstream_file.write_text(upstream_out)
    attempt_file.write_text("0")

    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
echo "docker:$*" >> "{trace}"
case "$1" in
  exec)
    if echo "$*" | grep -q "alembic_version_dalton"; then
      n=$(( $(cat "{attempt_file}") + 1 ))
      echo "$n" > "{attempt_file}"
      if [ "{sleep_dalton_s}" != "0" ]; then
        exec sleep {sleep_dalton_s}
      fi
      if [ {fail_dalton_until_attempt} -gt 0 ] && [ "$n" -le {fail_dalton_until_attempt} ]; then
        exit 1
      fi
      cat "{dalton_file}"
      exit {dalton_rc}
    elif echo "$*" | grep -q "alembic_version"; then
      cat "{upstream_file}"
      exit {upstream_rc}
    elif echo "$*" | grep -q "conversation_threads"; then
      echo "0"
      exit 0
    fi
    ;;
esac
exit 0
"""
    )
    (fake_bin / "docker").chmod(0o755)
    py_link = fake_bin / "python3"
    if not py_link.exists():
        py_link.symlink_to(_REPO_ROOT / ".venv/bin/python")
    return fake_bin, trace


def _run_revert_with_fake_docker(
    repo: Path,
    tmp_path: Path,
    *,
    pre_ff: str,
    migrating_deploy: str,
    backend_touched: str,
    crawler_touched: str = "1",
    edge_touched: str = "0",
    bridge_touched: str = "0",
    pre_ff_dalton_rev: str = "d040",
    pre_ff_upstream_rev: str = "146",
    cur_dalton_out: str = "d040\n",
    cur_upstream_out: str = "146\n",
    dalton_rc: int = 0,
    upstream_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    fake_bin, docker_trace = _make_fake_docker(
        tmp_path,
        dalton_out=cur_dalton_out,
        upstream_out=cur_upstream_out,
        dalton_rc=dalton_rc,
        upstream_rc=upstream_rc,
    )
    trace_log = tmp_path / "revert.trace"
    script = f"""
set -uo pipefail
export PATH="{fake_bin}:/bin:/usr/bin"
DALTON_DEPLOY_LIB_ONLY=1
DALTON_REPO_ROOT="{repo}"
DALTON_ALEMBIC_QUERY_RETRY_DELAY_S=0
source "{_DEPLOY_SCRIPT}"
cd "{repo}"
PRE_FF="{pre_ff}"
PRE_FF_DALTON_REV="{pre_ff_dalton_rev}"
PRE_FF_UPSTREAM_REV="{pre_ff_upstream_rev}"
MIGRATING_DEPLOY="{migrating_deploy}"
BACKEND_TOUCHED="{backend_touched}"
CRAWLER_TOUCHED="{crawler_touched}"
EDGE_TOUCHED="{edge_touched}"
BRIDGE_TOUCHED="{bridge_touched}"

uv() {{ echo "uv:$*" >> "{trace_log}"; return 0; }}
pnpm() {{ echo "pnpm:$*" >> "{trace_log}"; return 0; }}
bash() {{
  if [[ "${{1:-}}" == *"build_frontend_prod.sh"* ]]; then
    echo "build_frontend" >> "{trace_log}"
    echo "OK: Production bundle"
    return 0
  fi
  command bash "$@"
}}
apply_backend() {{ echo "apply_backend" >> "{trace_log}"; return 0; }}
apply_crawler() {{ echo "apply_crawler" >> "{trace_log}"; return 0; }}
recreate_edge() {{ echo "recreate_edge" >> "{trace_log}"; return 0; }}
wait_backend_health() {{ echo "wait_backend_health" >> "{trace_log}"; return 0; }}
wait_crawler_health() {{ echo "wait_crawler_health" >> "{trace_log}"; return 0; }}
wait_edge_ready() {{ echo "wait_edge_ready" >> "{trace_log}"; return 0; }}
run_public_edge_check() {{ echo "run_public_edge_check" >> "{trace_log}"; return 0; }}
launchctl() {{ echo "launchctl:$*" >> "{trace_log}"; return 0; }}

revert_deploy
"""
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    return res, trace_log, docker_trace


def test_parse_strict_migration_version_accepts_valid_and_rejects_malformed() -> None:
    def _run_parse(raw_expr: str, chain: str) -> subprocess.CompletedProcess[str]:
        script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
parse_strict_migration_version {raw_expr} "{chain}"
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)

    # Format-valid revisions are accepted regardless of whether a file exists in the working tree
    for raw_expr, chain, expected in [
        ("'d040'", "dalton", "d040"),
        ("$'d040\\n'", "dalton", "d040"),
        ("'d047'", "dalton", "d047"),
        ("'d999'", "dalton", "d999"),
        ("'146'", "upstream", "146"),
        ("$'146\\n'", "upstream", "146"),
        ("'14'", "upstream", "14"),
        ("'999'", "upstream", "999"),
        ("'abc12345'", "upstream", "abc12345"),
        ("'deadbeef'", "upstream", "deadbeef"),
    ]:
        res = _run_parse(raw_expr, chain)
        assert res.returncode == 0, f"expected valid for {raw_expr}: {res.stdout} {res.stderr}"
        assert res.stdout == expected

    invalid_cases = [
        ("''", "dalton"),
        ("$'\\n'", "dalton"),
        ("'   '", "dalton"),
        ("$'d040\\nd042'", "dalton"),
        ("$'d040\\nd042\\n'", "dalton"),
        ("$'d040\\n\\n'", "dalton"),
        ("$'\\nd040'", "dalton"),
        ("$'146\\n147\\n'", "upstream"),
        ("$'146\\n\\n'", "upstream"),
        ("'d040 (head)'", "dalton"),
        ("'d040 extra'", "dalton"),
        ("'d040 '", "dalton"),
        ("' d040'", "dalton"),
        ("$'d040\\tfoo'", "dalton"),
        ("$'d040\\r'", "dalton"),
        ("'146 (head)'", "upstream"),
        ("'146 trailing'", "upstream"),
        ("'d04'", "dalton"),
        ("'1234567'", "upstream"),
        ("'gggggggg'", "upstream"),
        ("'d*'", "dalton"),
        ("'*'", "upstream"),
        ("'../d040'", "dalton"),
        ("'d040'", "unknown"),
    ]
    for raw_expr, chain in invalid_cases:
        res = _run_parse(raw_expr, chain)
        assert res.returncode != 0, f"expected invalid for {raw_expr}, got rc=0 stdout={res.stdout!r}"
        assert res.stdout == ""


def test_get_alembic_versions_queries_full_table_and_retries_docker_exec_twice(
    tmp_path: Path,
) -> None:
    repo, _pre_ff, _head = _init_temp_repo(tmp_path)

    # Fails twice, succeeds on 3rd attempt (initial + 2 retries)
    fake_bin, trace = _make_fake_docker(
        tmp_path,
        dalton_out="d040\n",
        upstream_out="146\n",
        fail_dalton_until_attempt=2,
    )
    script = f"""
set -uo pipefail
export PATH="{fake_bin}:/bin:/usr/bin"
DALTON_DEPLOY_LIB_ONLY=1
DALTON_REPO_ROOT="{repo}"
DALTON_ALEMBIC_QUERY_RETRY_DELAY_S=0
source "{_DEPLOY_SCRIPT}"
d=$(get_dalton_alembic_version)
u=$(get_upstream_alembic_version)
printf '%s|%s' "$d" "$u"
"""
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert res.stdout == "d040|146"
    docker_calls = trace.read_text().splitlines()
    dalton_queries = [c for c in docker_calls if "alembic_version_dalton" in c]
    assert len(dalton_queries) == 3
    assert all("LIMIT" not in c.upper() for c in docker_calls)
    assert all("PGOPTIONS=-c statement_timeout=" in c for c in dalton_queries)

    # Fails 3 times (> 2 retries) -> fails without hanging
    fail_dir = tmp_path / "fail_case"
    fail_dir.mkdir()
    fake_bin_fail, trace_fail = _make_fake_docker(
        fail_dir,
        dalton_out="d040\n",
        upstream_out="146\n",
        fail_dalton_until_attempt=3,
    )
    fail_script = f"""
set -uo pipefail
export PATH="{fake_bin_fail}:/bin:/usr/bin"
DALTON_DEPLOY_LIB_ONLY=1
DALTON_REPO_ROOT="{repo}"
DALTON_ALEMBIC_QUERY_RETRY_DELAY_S=0
source "{_DEPLOY_SCRIPT}"
get_dalton_alembic_version
"""
    res_fail = subprocess.run(["bash", "-c", fail_script], capture_output=True, text=True, check=False)
    assert res_fail.returncode != 0
    assert res_fail.stdout == ""
    assert len([c for c in trace_fail.read_text().splitlines() if "alembic_version_dalton" in c]) == 3

    # Timeout via perl alarm stops hung docker exec and fails cleanly
    timeout_dir = tmp_path / "timeout_case"
    timeout_dir.mkdir()
    fake_bin_to, trace_to = _make_fake_docker(
        timeout_dir,
        dalton_out="d040\n",
        upstream_out="146\n",
        sleep_dalton_s=30,
    )
    to_script = f"""
set -uo pipefail
export PATH="{fake_bin_to}:/bin:/usr/bin"
DALTON_DEPLOY_LIB_ONLY=1
DALTON_REPO_ROOT="{repo}"
DALTON_ALEMBIC_QUERY_RETRY_DELAY_S=0
DALTON_ALEMBIC_QUERY_TIMEOUT_S=1
source "{_DEPLOY_SCRIPT}"
get_dalton_alembic_version
"""
    res_to = subprocess.run(
        ["bash", "-c", to_script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert res_to.returncode != 0
    assert res_to.stdout == ""
    assert len([c for c in trace_to.read_text().splitlines() if "alembic_version_dalton" in c]) == 3


def test_run_deploy_main_continues_when_pre_deploy_versions_unreadable(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    (seed / "src/dalton/migrations/versions").mkdir(parents=True)
    (seed / "migrations/versions").mkdir(parents=True)
    (seed / "src/dalton/migrations/versions/d040_baseline.py").write_text(
        "revision = 'd040'\ndown_revision = None\n"
    )
    (seed / "migrations/versions/146_baseline.py").write_text(
        "revision = '146'\ndown_revision = None\n"
    )
    (seed / "scripts/dalton").mkdir(parents=True)
    (seed / "web").mkdir(parents=True)
    (seed / "web/.keep").write_text("")
    (seed / "app.txt").write_text("v1\n")
    (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
        """#!/bin/bash
out_dir="web/dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --out-dir) out_dir="$2"; shift ;;
    --out-dir=*) out_dir="${1#--out-dir=}" ;;
  esac
  shift
done
mkdir -p "${out_dir}/assets" "${out_dir}/help"
echo '<!doctype html><div id="root"></div>' > "${out_dir}/index.html"
echo '{"build":"index-test.js"}' > "${out_dir}/version.json"
echo 'console.log("ok")' > "${out_dir}/assets/index-test.js"
echo '<h1>Help</h1>' > "${out_dir}/help/index.html"
echo 'OK: Production bundle'
exit 0
"""
    )
    (seed / "scripts/dalton/check_public_edge.sh").write_text("#!/bin/bash\necho 'edge ok'\nexit 0\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v1"], check=True)
    subprocess.run(["git", "-C", str(seed), "branch", "-M", "dalton/main"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "dalton/main"], check=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "-b", "dalton/main", str(origin), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)

    (seed / "app.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v2"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    new_head = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()

    fake_bin, _ = _make_fake_docker(tmp_path, dalton_out="", upstream_out="", dalton_rc=1, upstream_rc=1)
    backup_log = tmp_path / "pg-backup.log"
    backup_log.write_text("2026-11-20 00:00:00 ok prev\n")
    wait_idle = tmp_path / "wait_idle.sh"
    wait_idle.write_text("#!/bin/bash\nexit 0\n")
    wait_idle.chmod(0o755)

    (fake_bin / "launchctl").write_text(
        f"""#!/bin/bash
if [ "$1" = "kickstart" ]; then
  _nid_file="${{DALTON_PG_BACKUP_NEXT_ID_FILE:-$HOME/Library/Logs/.dalton-pg-backup.next-set-id}}"
  _sid="$(tr -d '[:space:]' < "$_nid_file" 2>/dev/null || true)"
  if [ -n "$_sid" ]; then
    _broot="{tmp_path}/mock-backups"
    mkdir -p "$_broot/pg" "$_broot/sets"
    _dump="$_broot/pg/dalton-pg-${{_sid}}.dump"
    printf 'PGDMP-%s' "$_sid" > "$_dump"
    _man="$_broot/sets/minio-manifest-${{_sid}}.json"
    printf '{{"version":2,"backup_set_id":"%s","objects":[]}}\\n' "$_sid" > "$_man"
    _dsha="$(shasum -a 256 "$_dump" | awk '{{print $1}}')"
    _dsz="$(wc -c < "$_dump" | tr -d '[:space:]')"
    _msha="$(shasum -a 256 "$_man" | awk '{{print $1}}')"
    printf '{{"version":1,"backup_set_id":"%s","dump_path":"%s","dump_size_bytes":%s,"dump_sha256":"%s","manifest_path":"%s","manifest_sha256":"%s","object_count":0,"reconcile_ok":true}}\\n' \\
      "$_sid" "$_dump" "$_dsz" "$_dsha" "$_man" "$_msha" > "$_broot/sets/backup-set-${{_sid}}.json"
    echo "2026-11-20 00:01:00 ok  16G ${{_dump}} set=${{_sid}}" >> "{backup_log}"
  else
    echo "2026-11-20 00:01:00 ok fresh" >> "{backup_log}"
  fi
fi
exit 0
"""
    )
    (fake_bin / "curl").write_text("#!/bin/bash\necho 200\nexit 0\n")
    (fake_bin / "uv").write_text("#!/bin/bash\nexit 0\n")
    (fake_bin / "pnpm").write_text("#!/bin/bash\nexit 0\n")
    for name in ("launchctl", "curl", "uv", "pnpm"):
        (fake_bin / name).chmod(0o755)

    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
WAIT_IDLE_SCRIPT="{wait_idle}"
run_deploy_main
"""
    res = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_PG_BACKUP_LOG": str(backup_log),
            "DALTON_ALEMBIC_QUERY_RETRY_DELAY_S": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "pre-deploy migrations: dalton=未能读取, upstream=未能读取" in res.stdout
    assert "DB version unreadable" in res.stdout
    assert f"deploy target: {new_head}" in res.stdout
    assert "migrating_deploy=1" in res.stdout
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == new_head


def test_compute_migrating_deploy_detects_migration_file_changes(tmp_path: Path) -> None:
    repo_no_mig, pre_ff_no_mig, head_no_mig = _init_temp_repo(tmp_path / "no_mig", with_migration_change=False)
    repo_with_mig, pre_ff_with_mig, head_with_mig = _init_temp_repo(tmp_path / "with_mig", with_migration_change=True)

    for repo, pre_ff, dalton_rev, upstream_rev, expected in [
        (repo_no_mig, pre_ff_no_mig, "d040", "146", "0"),
        (repo_with_mig, pre_ff_with_mig, "d040", "146", "1"),
        # Retry deploy: code already at target (PRE_FF == HEAD), DB still at old revisions -> migrating
        (repo_with_mig, head_with_mig, "d040", "146", "1"),
        # Unreadable DB version on code-only repo -> migrating
        (repo_no_mig, head_no_mig, "", "146", "1"),
        (repo_no_mig, head_no_mig, "d040", "", "1"),
    ]:
        script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
cd "{repo}"
PRE_FF="{pre_ff}"
PRE_FF_DALTON_REV="{dalton_rev}"
PRE_FF_UPSTREAM_REV="{upstream_rev}"
compute_migrating_deploy
printf '%s' "$MIGRATING_DEPLOY"
"""
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        assert res.returncode == 0, res.stderr
        assert res.stdout == expected
        if not dalton_rev or not upstream_rev:
            assert "unreadable" in res.stderr


def test_parse_deploy_args_validates_expect_migration_format() -> None:
    for bad_args in [
        "--expect-migration",
        "--expect-migration ''",
        "--expect-migration --bridge",
        "--expect-migration invalid",
        "--expect-migration d04",
        "--expect-migration 146",
        "--expect-migration d040a",
    ]:
        script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
parse_deploy_args {bad_args}
"""
        res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        assert res.returncode == 2, f"expected rc=2 for {bad_args}, got {res.returncode}"
        assert "Usage: deploy_main.sh" in res.stderr
        assert "--expect-migration" in res.stderr

    ok_script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
parse_deploy_args --bridge --expect-migration d040
printf '%s|%s' "$BRIDGE" "$EXPECT"
"""
    ok_res = subprocess.run(["bash", "-c", ok_script], capture_output=True, text=True, check=False)
    assert ok_res.returncode == 0
    assert ok_res.stdout == "1|d040"


def test_revert_deploy_aborts_without_reset_when_backend_untouched_with_migration(
    tmp_path: Path,
) -> None:
    repo, pre_ff, head = _init_temp_repo(tmp_path, with_migration_change=True)
    res, trace_log, docker_trace = _run_revert_with_fake_docker(
        repo,
        tmp_path,
        pre_ff=pre_ff,
        migrating_deploy="1",
        backend_touched="0",
        crawler_touched="0",
        edge_touched="0",
        pre_ff_dalton_rev="d040",
        pre_ff_upstream_rev="146",
        cur_dalton_out="d040\n",
        cur_upstream_out="146\n",
    )
    assert res.returncode != 0
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == head
    assert not trace_log.exists()
    d_text = docker_trace.read_text()
    assert "stop" not in d_text
    assert "downgrade" not in d_text
    out = res.stdout
    assert "AUTO-REVERT ABORTED" in out
    assert "后端可能已经或将会执行迁移" in out
    assert "后端已启动" not in out
    assert f"cd {repo}" in out
    assert "backend_touched=0, crawler_touched=0, edge_touched=0" in out


def test_revert_deploy_runs_original_flow_when_backend_untouched_without_migration(
    tmp_path: Path,
) -> None:
    repo, pre_ff, _head = _init_temp_repo(tmp_path, with_migration_change=False)
    res, trace_log, docker_trace = _run_revert_with_fake_docker(
        repo,
        tmp_path,
        pre_ff=pre_ff,
        migrating_deploy="0",
        backend_touched="0",
        crawler_touched="0",
        edge_touched="0",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "POST-REVERT REPORT: all revert steps passed" in res.stdout
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == pre_ff
    trace = trace_log.read_text().splitlines()
    assert "build_frontend" not in trace
    assert "apply_backend" not in trace
    assert "wait_backend_health" not in trace
    assert not docker_trace.exists()


def test_revert_deploy_runs_original_flow_when_backend_touched_without_migration(
    tmp_path: Path,
) -> None:
    repo, pre_ff, _head = _init_temp_repo(tmp_path, with_migration_change=False)
    res, trace_log, docker_trace = _run_revert_with_fake_docker(
        repo,
        tmp_path,
        pre_ff=pre_ff,
        migrating_deploy="0",
        backend_touched="1",
        crawler_touched="1",
        edge_touched="1",
        bridge_touched="1",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "POST-REVERT HEALTH REPORT: all revert steps and post-revert checks passed" in res.stdout
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == pre_ff
    trace = trace_log.read_text().splitlines()
    assert "build_frontend" not in trace
    assert "apply_backend" in trace
    assert "apply_crawler" in trace
    assert "recreate_edge" in trace
    assert "wait_backend_health" in trace
    assert "wait_crawler_health" in trace
    assert "wait_edge_ready" in trace
    assert "run_public_edge_check" in trace
    assert any(line.startswith("launchctl:kickstart") for line in trace)
    assert not docker_trace.exists()


def test_revert_deploy_aborts_without_reset_when_backend_touched_with_migration(
    tmp_path: Path,
) -> None:
    # Case 1: readable recorded and current versions (both changed)
    case1 = tmp_path / "readable"
    case1.mkdir()
    repo1, pre_ff1, head1 = _init_temp_repo(case1, with_migration_change=True)
    res1, trace1, docker1 = _run_revert_with_fake_docker(
        repo1,
        case1,
        pre_ff=pre_ff1,
        migrating_deploy="1",
        backend_touched="1",
        crawler_touched="1",
        edge_touched="0",
        bridge_touched="1",
        pre_ff_dalton_rev="d040",
        pre_ff_upstream_rev="146",
        cur_dalton_out="d041\n",
        cur_upstream_out="147\n",
    )
    assert res1.returncode != 0
    assert subprocess.check_output(["git", "-C", str(repo1), "rev-parse", "HEAD"], text=True).strip() == head1
    assert not trace1.exists()
    d1_text = docker1.read_text()
    assert "stop" not in d1_text
    assert "downgrade" not in d1_text
    assert "restart" not in d1_text

    out1 = res1.stdout
    assert "AUTO-REVERT ABORTED" in out1
    assert "后端可能已经或将会执行迁移" in out1
    assert "后端已启动" not in out1
    assert f"cd {repo1}" in out1
    assert "backend_touched=1, crawler_touched=1, edge_touched=0, bridge_touched=1" in out1
    assert "部署前=d040, 刚才读到=d041" in out1
    assert "部署前=146, 刚才读到=147" in out1
    assert f"PRE_FF ({pre_ff1}) 迁移链 head: dalton=d040, upstream=146" in out1
    assert f"TARGET ({head1}) 迁移链 head: dalton=d041, upstream=147" in out1
    # Recommended reset target is TARGET (head1) because its two heads match current DB (d041/147);
    # step 7 and step 8 both use the same RESET_SHA variable, and step 6 instructs updating RESET_SHA after downgrade.
    assert f"RESET_SHA={head1}" in out1
    assert 'target_migration_heads "$RESET_SHA"' in out1
    assert 'git reset --keep "$RESET_SHA"' in out1
    assert f"git reset --keep {head1}" not in out1
    assert f"git reset --keep {pre_ff1}" not in out1
    # After a downgrade the operator sets RESET_SHA once and re-runs only the check (7b),
    # so re-running a step never puts the pre-downgrade commit back into RESET_SHA.
    assert "跳过 7a，只执行 7b 核对，一致后再执行第 8 步。" in out1
    # Steps in order; plan B stops writes, reads DB, sets RESET_SHA (7a), verifies it (7b), then resets.
    labels = [f"{n}. " for n in range(1, 7)] + ["7a.", "7b.", "8. ", "9. "]
    positions = [out1.index(f"\n{label}") for label in labels]
    assert positions == sorted(positions)
    assert out1.index("docker stop dalton-langalpha-edge-1") < out1.index("SELECT version_num FROM alembic_version_dalton")
    assert out1.index("SELECT version_num FROM alembic_version_dalton") < out1.index(f"RESET_SHA={head1}")
    assert out1.index(f"RESET_SHA={head1}") < out1.index('target_migration_heads "$RESET_SHA"')
    assert out1.index('target_migration_heads "$RESET_SHA"') < out1.index('git reset --keep "$RESET_SHA"')
    assert "docker ps -a --filter name=dalton-langalpha-" in out1
    assert "docker logs --tail 100 dalton-langalpha-backend-1" in out1
    assert "docker compose config backend | grep -E 'DB_(HOST|PORT|NAME)'" in out1
    assert "docker compose config backend" in out1
    assert "SELECT version_num FROM alembic_version_dalton" in out1
    assert "SELECT version_num FROM alembic_version" in out1
    assert "dalton-pg-backup.log" in out1
    assert "com.dalton.monitor-feeds" in out1
    assert "com.dalton.researchdata-bridge" in out1
    assert "com.dalton.channel-gateway" in out1
    assert "不用 docker exec" in out1
    assert "维护容器" in out1
    assert "launchctl bootout gui/$(id -u)/com.dalton.backend-watchdog" in out1
    assert "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dalton.backend-watchdog.plist" in out1
    assert "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dalton.researchdata-bridge.plist" in out1
    assert "crawler:8100/health" in out1
    assert "check_public_edge.sh" in out1
    assert "src/dalton/alembic.ini downgrade d040" in out1
    assert "alembic.ini downgrade 146" in out1

    # Case 2: unreadable pre-deploy and current versions -> still aborts without reset, shows "未能读取" & recovery hint
    case2 = tmp_path / "unreadable"
    case2.mkdir()
    repo2, pre_ff2, head2 = _init_temp_repo(case2, with_migration_change=True)
    res2, trace2, _ = _run_revert_with_fake_docker(
        repo2,
        case2,
        pre_ff=pre_ff2,
        migrating_deploy="1",
        backend_touched="1",
        pre_ff_dalton_rev="",
        pre_ff_upstream_rev="",
        cur_dalton_out="",
        cur_upstream_out="",
        dalton_rc=1,
        upstream_rc=1,
    )
    assert res2.returncode != 0
    assert subprocess.check_output(["git", "-C", str(repo2), "rev-parse", "HEAD"], text=True).strip() == head2
    assert not trace2.exists()
    assert "部署前=未能读取, 刚才读到=未能读取" in res2.stdout
    assert "版本读不到" in res2.stdout
    assert "先不要重启后端" in res2.stdout
    assert "RESET_SHA=<已核对一致的提交>" in res2.stdout
    assert 'target_migration_heads "$RESET_SHA"' in res2.stdout
    assert 'git reset --keep "$RESET_SHA"' in res2.stdout
    assert f"git reset --keep {pre_ff2}" not in res2.stdout
    assert "部署前的备份" in res2.stdout
    assert "\n1. " in res2.stdout and "\n9. " in res2.stdout


def test_deploy_scripts_have_no_lock_or_auto_downgrade_or_model_specific_wording() -> None:
    assert not _OPS_LOCK_SCRIPT.exists()
    for path in (_DEPLOY_SCRIPT, _WATCHDOG_SCRIPT):
        text = path.read_text()
        for forbidden in (
            "_ops_lock",
            "stop_writes_before_downgrade",
            "run_dalton_alembic_downgrade",
            "run_upstream_alembic_downgrade",
            "restore_services_after_downgrade_abort",
            "run_backend_oneshot_alembic",
            "d041",
            "覆盖",
        ):
            assert forbidden not in text, f"unexpected {forbidden!r} in {path}"


def test_recreate_saves_container_logs_before_replacing_containers(tmp_path: Path) -> None:
    import gzip
    import os
    import time

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trace = tmp_path / "docker.trace"
    log_dir = tmp_path / "container-logs"
    log_dir.mkdir()
    stale_gz = log_dir / "backend-20200101T000000Z.log.gz"
    stale_gz.write_bytes(b"stale")
    old_mtime = time.time() - 20 * 86400
    os.utime(stale_gz, (old_mtime, old_mtime))

    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
echo "docker:$*" >> "{trace}"
case "$1" in
  ps)
    echo "abc123"
    exit 0
    ;;
  logs)
    echo "2026-11-20T12:00:00Z sample container log line"
    exit 0
    ;;
  compose)
    exit 0
    ;;
esac
exit 0
"""
    )
    (fake_bin / "docker").chmod(0o755)

    res = subprocess.run(
        [
            "bash",
            "-c",
            f'DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}" && RECREATE=1 && '
            f'save_calls=0; orig_fn=$(declare -f save_container_logs); '
            f'eval "real_${{orig_fn}}"; '
            f'save_container_logs() {{ save_calls=$((save_calls + 1)); real_save_container_logs "$@"; }}; '
            f'apply_backend && apply_crawler && echo "save_calls=$save_calls"',
        ],
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "DALTON_CONTAINER_LOG_DIR": str(log_dir),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stderr + res.stdout
    assert "save_calls=2" in res.stdout

    trace_lines = trace.read_text().splitlines()
    backend_logs_idx = trace_lines.index("docker:logs --timestamps dalton-langalpha-backend-1")
    backend_up_idx = trace_lines.index("docker:compose up -d --no-deps backend")
    crawler_logs_idx = trace_lines.index("docker:logs --timestamps dalton-langalpha-crawler-1")
    crawler_up_idx = trace_lines.index("docker:compose up -d --no-deps --no-build crawler")
    assert backend_logs_idx < backend_up_idx
    assert crawler_logs_idx < crawler_up_idx

    assert not stale_gz.exists()
    backend_files = list(log_dir.glob("backend-*.log.gz"))
    crawler_files = list(log_dir.glob("crawler-*.log.gz"))
    assert len(backend_files) == 1
    assert len(crawler_files) == 1
    assert "sample container log line" in gzip.decompress(backend_files[0].read_bytes()).decode("utf-8")

    # Failure to dump logs only warns and does not abort deploy
    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
echo "docker:$*" >> "{trace}"
case "$1" in
  ps)
    echo "abc123"
    exit 0
    ;;
  logs)
    echo "no such container" >&2
    exit 1
    ;;
  compose)
    exit 0
    ;;
esac
exit 0
"""
    )
    (fake_bin / "docker").chmod(0o755)
    res_warn = subprocess.run(
        [
            "bash",
            "-c",
            f'DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}" && RECREATE=1 && apply_backend',
        ],
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "DALTON_CONTAINER_LOG_DIR": str(log_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_warn.returncode == 0, res_warn.stderr + res_warn.stdout
    assert "WARN:" in res_warn.stderr


def test_cleanup_logs_only_removes_generated_archives_and_edge_saves_logs(
    tmp_path: Path,
) -> None:
    """Cleanup removes only `<service>-<UTC timestamp>.log.gz`, and `recreate_edge` saves edge logs."""
    import os

    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    gen_backend = log_dir / "backend-20261120T010203Z.log.gz"
    gen_edge = log_dir / "edge-20261120T010203Z.log.gz"
    unrelated_gz = log_dir / "custom-backup.log.gz"
    unrelated_other = log_dir / "backend-note.txt"
    for f in (gen_backend, gen_edge, unrelated_gz, unrelated_other):
        f.write_bytes(b"data")

    script_text = _DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "-name 'backend-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.log.gz'" in script_text
    assert "-name 'edge-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.log.gz'" in script_text

    cleanup_cmd = (
        f"find {log_dir!s} -maxdepth 1 -type f \\( "
        "-name 'backend-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.log.gz' "
        "-o -name 'crawler-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.log.gz' "
        "-o -name 'edge-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.log.gz' "
        "\\) -delete"
    )
    res = subprocess.run(["bash", "-c", cleanup_cmd], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert not gen_backend.exists()
    assert not gen_edge.exists()
    assert unrelated_gz.exists()
    assert unrelated_other.exists()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "docker_calls.txt"
    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
echo "$*" >> "{call_log}"
case "$1" in
  ps)
    echo "cid-edge-123"
    ;;
  logs)
    echo "edge log line"
    ;;
  compose)
    exit 0
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    (fake_bin / "docker").chmod(0o755)
    edge_res = subprocess.run(
        ["bash", "-c", f'DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}" && recreate_edge'],
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "DALTON_CONTAINER_LOG_DIR": str(log_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert edge_res.returncode == 0, edge_res.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    logs_idx = next(i for i, line in enumerate(calls) if "logs --timestamps dalton-langalpha-edge-1" in line)
    recreate_idx = next(i for i, line in enumerate(calls) if "--force-recreate edge" in line)
    assert logs_idx < recreate_idx
    assert list(log_dir.glob("edge-*Z.log.gz"))


def test_run_deploy_main_cleans_old_container_logs_even_without_recreate(
    tmp_path: Path,
) -> None:
    """Every deploy cleans >14-day generated container log archives at startup even on normal restart."""
    import os
    import time

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    (seed / "src/dalton/migrations/versions").mkdir(parents=True)
    (seed / "migrations/versions").mkdir(parents=True)
    (seed / "src/dalton/migrations/versions/d040_baseline.py").write_text(
        "revision = 'd040'\ndown_revision = None\n"
    )
    (seed / "migrations/versions/146_baseline.py").write_text(
        "revision = '146'\ndown_revision = None\n"
    )
    (seed / "scripts/dalton").mkdir(parents=True)
    (seed / "web").mkdir(parents=True)
    (seed / "web/.keep").write_text("")
    (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
        """#!/bin/bash
out_dir="web/dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --out-dir) out_dir="$2"; shift ;;
    --out-dir=*) out_dir="${1#--out-dir=}" ;;
  esac
  shift
done
mkdir -p "${out_dir}/assets" "${out_dir}/help"
echo '<!doctype html><div id="root"></div>' > "${out_dir}/index.html"
echo '{"build":"index-test.js"}' > "${out_dir}/version.json"
echo 'console.log("ok")' > "${out_dir}/assets/index-test.js"
echo '<h1>Help</h1>' > "${out_dir}/help/index.html"
echo 'OK: Production bundle'
exit 0
"""
    )
    (seed / "scripts/dalton/check_public_edge.sh").write_text("#!/bin/bash\necho 'edge ok'\nexit 0\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v1"], check=True)
    subprocess.run(["git", "-C", str(seed), "branch", "-M", "dalton/main"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "dalton/main"], check=True)

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "-b", "dalton/main", str(origin), str(repo)], check=True)

    fake_bin, _ = _make_fake_docker(tmp_path, dalton_out="", upstream_out="", dalton_rc=1, upstream_rc=1)
    backup_log = tmp_path / "pg-backup.log"
    backup_log.write_text("2026-11-20 00:00:00 ok prev\n")
    wait_idle = tmp_path / "wait_idle.sh"
    wait_idle.write_text("#!/bin/bash\nexit 0\n")
    wait_idle.chmod(0o755)
    (fake_bin / "launchctl").write_text(
        f"""#!/bin/bash
if [ "$1" = "kickstart" ]; then
  _nid_file="${{DALTON_PG_BACKUP_NEXT_ID_FILE:-$HOME/Library/Logs/.dalton-pg-backup.next-set-id}}"
  _sid="$(tr -d '[:space:]' < "$_nid_file" 2>/dev/null || true)"
  if [ -n "$_sid" ]; then
    _broot="{tmp_path}/mock-backups-logs"
    mkdir -p "$_broot/pg" "$_broot/sets"
    _dump="$_broot/pg/dalton-pg-${{_sid}}.dump"
    printf 'PGDMP-%s' "$_sid" > "$_dump"
    _man="$_broot/sets/minio-manifest-${{_sid}}.json"
    printf '{{"version":2,"backup_set_id":"%s","objects":[]}}\\n' "$_sid" > "$_man"
    _dsha="$(shasum -a 256 "$_dump" | awk '{{print $1}}')"
    _dsz="$(wc -c < "$_dump" | tr -d '[:space:]')"
    _msha="$(shasum -a 256 "$_man" | awk '{{print $1}}')"
    printf '{{"version":1,"backup_set_id":"%s","dump_path":"%s","dump_size_bytes":%s,"dump_sha256":"%s","manifest_path":"%s","manifest_sha256":"%s","object_count":0,"reconcile_ok":true}}\\n' \\
      "$_sid" "$_dump" "$_dsz" "$_dsha" "$_man" "$_msha" > "$_broot/sets/backup-set-${{_sid}}.json"
    echo "2026-11-20 00:01:00 ok  16G ${{_dump}} set=${{_sid}}" >> "{backup_log}"
  else
    echo "2026-11-20 00:01:00 ok fresh" >> "{backup_log}"
  fi
fi
exit 0
"""
    )
    (fake_bin / "curl").write_text("#!/bin/bash\necho 200\nexit 0\n")
    (fake_bin / "uv").write_text("#!/bin/bash\nexit 0\n")
    (fake_bin / "pnpm").write_text("#!/bin/bash\nexit 0\n")
    for name in ("launchctl", "curl", "uv", "pnpm"):
        (fake_bin / name).chmod(0o755)

    log_dir = tmp_path / "container-logs"
    log_dir.mkdir()
    stale_backend = log_dir / "backend-20200101T000000Z.log.gz"
    fresh_backend = log_dir / "backend-20261120T000000Z.log.gz"
    unrelated = log_dir / "manual-keep.log.gz"
    for f in (stale_backend, fresh_backend, unrelated):
        f.write_bytes(b"data")
    old_mtime = time.time() - 20 * 86400
    os.utime(stale_backend, (old_mtime, old_mtime))
    os.utime(unrelated, (old_mtime, old_mtime))

    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
WAIT_IDLE_SCRIPT="{wait_idle}"
run_deploy_main
"""
    res = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_PG_BACKUP_LOG": str(backup_log),
            "DALTON_CONTAINER_LOG_DIR": str(log_dir),
            "DALTON_ALEMBIC_QUERY_RETRY_DELAY_S": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert not stale_backend.exists()
    assert fresh_backend.exists()
    assert unrelated.exists()


def test_revert_deploy_refuses_auto_revert_when_pre_ff_has_unapplied_migration(
    tmp_path: Path,
) -> None:
    """Scenario: previous build failed leaving local checkout on commit with d047 while DB stayed
    at d046; remote later reverted d047, so the new deploy has MIGRATING_DEPLOY=0, then fails
    at public edge check. Auto-revert must be refused because PRE_FF (d047) != DB (d046).
    """
    from datetime import datetime

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    (seed / "src/dalton/migrations/versions").mkdir(parents=True)
    (seed / "migrations/versions").mkdir(parents=True)
    (seed / "src/dalton/migrations/versions/d046_base.py").write_text(
        "revision = 'd046'\ndown_revision = None\n"
    )
    (seed / "migrations/versions/146_base.py").write_text(
        "revision = '146'\ndown_revision = None\n"
    )
    (seed / "scripts/dalton").mkdir(parents=True)
    (seed / "web").mkdir(parents=True)
    (seed / "web/.keep").write_text("")
    (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
        """#!/bin/bash
out_dir="web/dist"
while [ $# -gt 0 ]; do
  case "$1" in
    --out-dir) out_dir="$2"; shift ;;
    --out-dir=*) out_dir="${1#--out-dir=}" ;;
  esac
  shift
done
mkdir -p "${out_dir}/assets" "${out_dir}/help"
echo '<!doctype html><div id="root"></div>' > "${out_dir}/index.html"
echo '{"build":"index-test.js"}' > "${out_dir}/version.json"
echo 'console.log("ok")' > "${out_dir}/assets/index-test.js"
echo '<h1>Help</h1>' > "${out_dir}/help/index.html"
echo 'OK: Production bundle'
exit 0
"""
    )
    (seed / "scripts/dalton/check_public_edge.sh").write_text("#!/bin/bash\necho 'edge fail'\nexit 1\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v1 base d046"], check=True)
    subprocess.run(["git", "-C", str(seed), "branch", "-M", "dalton/main"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "dalton/main"], check=True)

    # v2 adds d047; local checkout lands on v2 (as after a previous failed frontend build)
    (seed / "src/dalton/migrations/versions/d047_new.py").write_text(
        "revision = 'd047'\ndown_revision = 'd046'\n"
    )
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v2 add d047"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    v2_with_d047 = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "-b", "dalton/main", str(origin), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == v2_with_d047

    # v3 on remote reverts d047 so target matches DB (d046/146)
    (seed / "src/dalton/migrations/versions/d047_new.py").unlink()
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v3 revert d047"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    v3_reverted = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()

    fake_bin, docker_trace = _make_fake_docker(
        tmp_path,
        dalton_out="d046\n",
        upstream_out="146\n",
    )
    backup_log = tmp_path / "pg-backup.log"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    backup_log.write_text(f"{now_str} ok  16G /backups/recent.dump\n")
    wait_idle = tmp_path / "wait_idle.sh"
    wait_idle.write_text("#!/bin/bash\nexit 0\n")
    wait_idle.chmod(0o755)
    (fake_bin / "launchctl").write_text("#!/bin/bash\nexit 0\n")
    (fake_bin / "curl").write_text("#!/bin/bash\necho 200\nexit 0\n")
    (fake_bin / "uv").write_text("#!/bin/bash\nexit 0\n")
    (fake_bin / "pnpm").write_text("#!/bin/bash\nexit 0\n")
    for name in ("launchctl", "curl", "uv", "pnpm"):
        (fake_bin / name).chmod(0o755)

    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
WAIT_IDLE_SCRIPT="{wait_idle}"
run_deploy_main --reuse-backup-minutes 60
"""
    res = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_PG_BACKUP_LOG": str(backup_log),
            "DALTON_ALEMBIC_QUERY_RETRY_DELAY_S": "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "backup: reusing" in res.stdout
    assert "migrating_deploy=0" in res.stdout
    assert "AUTO-REVERT ABORTED" in res.stdout
    assert "回退目标 PRE_FF 的迁移版本与数据库不一致" in res.stdout
    assert "本次发布包含数据库迁移文件" not in res.stdout
    assert f"PRE_FF ({v2_with_d047}) 迁移链 head: dalton=d047, upstream=146" in res.stdout
    assert f"TARGET ({v3_reverted}) 迁移链 head: dalton=d046, upstream=146" in res.stdout
    assert f"RESET_SHA={v3_reverted}" in res.stdout
    assert 'target_migration_heads "$RESET_SHA"' in res.stdout
    assert 'git reset --keep "$RESET_SHA"' in res.stdout
    assert f"git reset --keep {v2_with_d047}" not in res.stdout
    assert (
        res.stdout.index(f"RESET_SHA={v3_reverted}")
        < res.stdout.index('target_migration_heads "$RESET_SHA"')
        < res.stdout.index('git reset --keep "$RESET_SHA"')
    )
    assert "PRE_FF" in res.stderr and "d047" in res.stderr
    # Checkout stays on v3_reverted and must NOT reset back to v2_with_d047
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == v3_reverted
    # Backend was restarted once during deploy, never a second time for revert
    restarts = [l for l in docker_trace.read_text().splitlines() if "restart dalton-langalpha-backend-1" in l]
    assert len(restarts) == 1


def test_revert_deploy_manual_instructions_when_neither_pre_ff_nor_target_matches_db(
    tmp_path: Path,
) -> None:
    """When neither PRE_FF nor TARGET matches the DB revisions, manual instructions warn not to
    restart backend first and provide step-by-step commands to confirm the commit to deploy.
    """
    repo, pre_ff, head = _init_temp_repo(tmp_path, with_migration_change=True)
    res, trace_log, _docker_trace = _run_revert_with_fake_docker(
        repo,
        tmp_path,
        pre_ff=pre_ff,
        migrating_deploy="1",
        backend_touched="1",
        pre_ff_dalton_rev="d039",
        pre_ff_upstream_rev="146",
        cur_dalton_out="d039\n",
        cur_upstream_out="146\n",
    )
    assert res.returncode != 0
    assert not trace_log.exists()
    out = res.stdout
    assert "AUTO-REVERT ABORTED" in out
    assert f"PRE_FF ({pre_ff}) 迁移链 head: dalton=d040, upstream=146" in out
    assert f"TARGET ({head}) 迁移链 head: dalton=d041, upstream=147" in out
    assert "刚才读到=d039" in out
    assert "先不要重启后端" in out
    assert f"target_migration_heads {pre_ff}" in out
    assert f"target_migration_heads {head}" in out
    assert "git log --oneline -n 10" in out
    assert "RESET_SHA=<已核对一致的提交>" in out
    assert 'target_migration_heads "$RESET_SHA"' in out
    assert 'git reset --keep "$RESET_SHA"' in out
    assert f"git reset --keep {pre_ff}" not in out
    assert f"git reset --keep {head}" not in out
    assert (
        out.index("RESET_SHA=<已核对一致的提交>")
        < out.index('target_migration_heads "$RESET_SHA"')
        < out.index('git reset --keep "$RESET_SHA"')
    )

