"""Unit tests for channel-gateway release build, atomic symlink switch, tree tracking, health check, and rollback."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_SCRIPT = _REPO_ROOT / "scripts/dalton/ops/prod/deploy_main.sh"
_GATEWAY_GITIGNORE = _REPO_ROOT / "integrations/dalton-channel-gateway/.gitignore"
_SECRET_TOKEN = "gw-secret-token-never-print-0123456789abcdef"

_GITIGNORE_CONTENT = (
    "node_modules\n"
    "node_modules/\n"
    "dist\n"
    "dist/\n"
    ".deploy-releases/\n"
    ".deploy-current\n"
    ".deploy-current.*\n"
    ".deploy-deployed-tree\n"
    ".deploy-deployed-tree.*\n"
    ".deploy-legacy-backup-*\n"
    ".deploy-failed.*\n"
    ".deploy-gateway-pending\n"
    ".deploy-gateway-pending.*\n"
    ".deploy-staging/\n"
    ".deploy-prev/\n"
)


def _setup_gateway_deploy_env(
    tmp_path: Path,
    *,
    gateway_changed: bool = True,
    with_migration_change: bool = False,
    gateway_pnpm_rc: int = 0,
    gateway_node_build_behavior: str = "success",
    gateway_staging_import_rc: int = 0,
    gateway_import_rc: int = 0,
    gateway_kickstart_mode: str = "ok",
    gateway_health_mode: str = "ready_true",
    stale_staging: bool = False,
    stale_prev: bool = False,
    wait_idle_fail_after_first: bool = False,
    git_diff_error_rc: int = 0,
    git_rev_parse_gw_error_rc: int = 0,
    crawler_health_fail: bool = False,
) -> dict[str, Path | str]:
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
    gw_seed = seed / "integrations/dalton-channel-gateway"
    (gw_seed / "src").mkdir(parents=True)
    (gw_seed / "package.json").write_text('{"name":"@dalton/channel-gateway","type":"module"}\n')
    (gw_seed / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (gw_seed / "build.mjs").write_text("// build script\n")
    (gw_seed / "tsconfig.json").write_text('{"compilerOptions":{}}\n')
    (gw_seed / "src/main.ts").write_text("export const v = 1;\n")
    (gw_seed / ".gitignore").write_text(_GITIGNORE_CONTENT)
    (seed / "app.py").write_text("v1\n")

    events_trace = tmp_path / "events.trace"
    (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
        f'#!/bin/bash\necho \'frontend_build\' >> "{events_trace}"\necho \'OK: Production bundle\'\nexit 0\n'
    )
    (seed / "scripts/dalton/check_public_edge.sh").write_text(
        "#!/bin/bash\necho 'edge ok'\nexit 0\n"
    )

    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v1"], check=True)
    subprocess.run(["git", "-C", str(seed), "branch", "-M", "dalton/main"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "dalton/main"], check=True)
    v1 = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()
    v1_gw_tree = subprocess.check_output(
        ["git", "-C", str(seed), "rev-parse", f"{v1}:integrations/dalton-channel-gateway"],
        text=True,
    ).strip()

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "-b", "dalton/main", str(origin), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)

    # Pre-existing legacy production dist/ and node_modules/ (real directories, as in existing prod checkout)
    gw_dir = repo / "integrations/dalton-channel-gateway"
    (gw_dir / "dist").mkdir(parents=True, exist_ok=True)
    (gw_dir / "dist/index.mjs").write_text("v1-bundle\n")
    (gw_dir / "node_modules/@larksuiteoapi/node-sdk").mkdir(parents=True, exist_ok=True)
    (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").write_text("v1-sdk\n")
    if stale_staging:
        (gw_dir / ".deploy-staging").mkdir(parents=True, exist_ok=True)
        (gw_dir / ".deploy-staging/stale.txt").write_text("stale-staging\n")
    if stale_prev:
        (gw_dir / ".deploy-prev").mkdir(parents=True, exist_ok=True)
        (gw_dir / ".deploy-prev/stale.txt").write_text("stale-prev\n")

    # Push v2
    if gateway_changed:
        (gw_seed / "src/main.ts").write_text("export const v = 2;\n")
    else:
        (seed / "app.py").write_text("v2\n")
    if with_migration_change:
        (seed / "src/dalton/migrations/versions/d042_next.py").write_text(
            "revision = 'd042'\ndown_revision = 'd040'\n"
        )
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v2"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    v2 = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()
    v2_gw_tree = subprocess.check_output(
        ["git", "-C", str(seed), "rev-parse", f"{v2}:integrations/dalton-channel-gateway"],
        text=True,
    ).strip()

    # Gateway env file under fake HOME
    env_dir = tmp_path / ".config/dalton"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / "channel-gateway.env"
    env_file.write_text(f'# gateway service token\nexport INTERNAL_SERVICE_TOKEN="{_SECRET_TOKEN}"\n')
    env_file.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    backup_log = tmp_path / "pg-backup.log"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    backup_log.write_text(f"{now_str} ok  16G /backups/recent.dump\n")

    wait_idle_calls = tmp_path / "wait_idle.calls"
    wait_idle_calls.write_text("0")
    wait_idle = tmp_path / "wait_idle.sh"
    if wait_idle_fail_after_first:
        wait_idle.write_text(
            f"""#!/bin/bash
n=$(( $(cat "{wait_idle_calls}") + 1 ))
echo "$n" > "{wait_idle_calls}"
echo "wait_idle:$n" >> "{events_trace}"
if [ "$n" -gt 1 ]; then
  exit 1
fi
exit 0
"""
        )
    else:
        wait_idle.write_text("#!/bin/bash\nexit 0\n")
    wait_idle.chmod(0o755)

    kickstart_calls = tmp_path / "gw_kickstart.calls"
    kickstart_calls.write_text("0")
    if gateway_kickstart_mode == "fail_first":
        gw_ks_body = f"""
  n=$(( $(cat "{kickstart_calls}") + 1 ))
  echo "$n" > "{kickstart_calls}"
  if [ "$n" -eq 1 ]; then
    exit 1
  fi
"""
    elif gateway_kickstart_mode == "always_fail":
        gw_ks_body = "  exit 1\n"
    else:
        gw_ks_body = "  :\n"

    (fake_bin / "launchctl").write_text(
        f"""#!/bin/bash
echo "launchctl:$*" >> "{events_trace}"
if [[ "$*" == *"com.dalton.pg-backup"* ]]; then
  printf '%s ok  16G /backups/fresh-%s.dump\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$$" >> "{backup_log}"
fi
if [ "$1" = "kickstart" ] && [[ "$*" == *"com.dalton.channel-gateway"* ]]; then
{gw_ks_body}
fi
exit 0
"""
    )

    busy_return = "1" if wait_idle_fail_after_first else "0"
    crawler_curl_rc = "1" if crawler_health_fail else "0"
    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
echo "docker:$*" >> "{events_trace}"
case "$1" in
  compose)
    if [[ "$*" == *"config --services"* ]]; then
      echo "backend"
      echo "crawler"
      exit 0
    fi
    ;;
  exec)
    if echo "$*" | grep -q "alembic_version_dalton"; then
      echo "d040"
      exit 0
    elif echo "$*" | grep -q "alembic_version"; then
      echo "146"
      exit 0
    elif echo "$*" | grep -q "conversation_threads"; then
      echo "{busy_return}"
      exit 0
    elif echo "$*" | grep -q "http://crawler:8100/health"; then
      if [ "{crawler_curl_rc}" = "0" ]; then
        echo "crawler health 200"
        exit 0
      fi
      exit 1
    fi
    ;;
esac
exit 0
"""
    )

    (fake_bin / "uv").write_text("#!/bin/bash\nexit 0\n")

    (fake_bin / "pnpm").write_text(
        f"""#!/bin/bash
echo "pnpm:$(pwd):$*" >> "{events_trace}"
if [[ "$(pwd)" == *"integrations/dalton-channel-gateway"* ]]; then
  if [ {gateway_pnpm_rc} -ne 0 ]; then
    exit {gateway_pnpm_rc}
  fi
  ver_tag="v2"
  if [ -f src/main.ts ] && grep -q "v = 3" src/main.ts; then
    ver_tag="v3"
  fi
  mkdir -p node_modules/@larksuiteoapi/node-sdk
  echo "${{ver_tag}}-sdk" > node_modules/@larksuiteoapi/node-sdk/index.js
fi
exit 0
"""
    )

    if gateway_node_build_behavior == "success":
        node_build_body = """
  ver_tag="v2"
  if [ -f src/main.ts ] && grep -q "v = 3" src/main.ts; then
    ver_tag="v3"
  fi
  mkdir -p dist
  echo "${ver_tag}-bundle" > dist/index.mjs
  exit 0
"""
    else:
        node_build_body = """
  mkdir -p dist
  echo "corrupt-partial-v2" > dist/index.mjs
  echo "esbuild failed" >&2
  exit 1
"""

    real_py = str(_REPO_ROOT / ".venv/bin/python")
    (fake_bin / "node").write_text(
        f"""#!/bin/bash
if [ "${{1:-}}" = "build.mjs" ]; then
  echo "node_build:$(pwd):$*" >> "{events_trace}"
{node_build_body}
fi
if [[ "$*" == *"--input-type=module"* ]]; then
  check_dir="$(pwd)"
  trace_dir="$check_dir"
  if [[ "${{!#}}" == */dist/index.mjs ]]; then
    entry_arg="${{!#}}"
    check_dir="$(dirname "$(dirname "$entry_arg")")"
    nm_target="$(readlink "${{check_dir}}/node_modules" 2>/dev/null || true)"
    if [ -n "$nm_target" ]; then
      trace_dir="$(dirname "$nm_target")"
    fi
  fi
  echo "node_import:${{trace_dir}}" >> "{events_trace}"
  if [[ "$trace_dir" == *".deploy-releases/"* ]]; then
    if [ {gateway_staging_import_rc} -ne 0 ]; then
      echo "ERR_MODULE_NOT_FOUND in staging release" >&2
      exit {gateway_staging_import_rc}
    fi
  else
    if [ {gateway_import_rc} -ne 0 ]; then
      echo "ERR_MODULE_NOT_FOUND: @larksuiteoapi/node-sdk" >&2
      exit {gateway_import_rc}
    fi
  fi
  # Simulate Node ESM realpath resolution: resolve realpath of dist/index.mjs,
  # walk up from realpath(dist/index.mjs) to sibling node_modules/@larksuiteoapi/node-sdk/index.js,
  # verify it matches realpath(node_modules/@larksuiteoapi/node-sdk/index.js),
  # and verify bundle and sdk belong to the same release version.
  (
    cd "$check_dir" && "{real_py}" -c '
import os, sys
entry = "dist/index.mjs"
sdk = "node_modules/@larksuiteoapi/node-sdk/index.js"
if not (os.path.isfile(entry) and os.path.isfile(sdk)):
    sys.exit(1)
real_entry = os.path.realpath(entry)
real_sdk_from_cwd = os.path.realpath(sdk)
release_root = os.path.dirname(os.path.dirname(real_entry))
resolved_sdk = os.path.join(release_root, "node_modules", "@larksuiteoapi", "node-sdk", "index.js")
if not os.path.isfile(resolved_sdk):
    print(f"ERR_MODULE_NOT_FOUND: {{resolved_sdk}} missing next to {{real_entry}}", file=sys.stderr)
    sys.exit(1)
if os.path.realpath(resolved_sdk) != real_sdk_from_cwd:
    print(f"ERR_SYMLINK_MISMATCH: {{resolved_sdk}} != {{real_sdk_from_cwd}}", file=sys.stderr)
    sys.exit(1)
with open(real_entry) as f:
    b_ver = f.read().strip().split("-", 1)[0]
with open(resolved_sdk) as f:
    s_ver = f.read().strip().split("-", 1)[0]
if b_ver != s_ver:
    print(f"ERR_VERSION_PAIR_MISMATCH: bundle={{b_ver}} sdk={{s_ver}}", file=sys.stderr)
    sys.exit(1)
'
  ) || exit 1
  exit 0
fi
echo "node_other:$(pwd):$*" >> "{events_trace}"
exit 0
"""
    )

    gw_dist_file = gw_dir / "dist/index.mjs"
    gw_sdk_file = gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js"
    if gateway_health_mode == "ready_true":
        gw_health_snippet = """
  echo '{"ok":true,"ready":true}'
  exit 0
"""
    elif gateway_health_mode == "ok_only":
        gw_health_snippet = """
  echo '{"ok":true}'
  exit 0
"""
    elif gateway_health_mode == "ready_false_until_restored":
        gw_health_snippet = f"""
  if [ -f "{gw_dist_file}" ] && grep -qx "v1-bundle" "{gw_dist_file}" \\
     && [ -f "{gw_sdk_file}" ] && grep -qx "v1-sdk" "{gw_sdk_file}"; then
    echo '{{"ok":true,"ready":true}}'
  else
    echo '{{"ok":true,"ready":false}}'
  fi
  exit 0
"""
    elif gateway_health_mode == "always_ready_false":
        gw_health_snippet = """
  echo '{"ok":true,"ready":false}'
  exit 0
"""
    else:
        gw_health_snippet = "exit 1\n"

    (fake_bin / "curl").write_text(
        f"""#!/bin/bash
if [[ "$*" == *"127.0.0.1:8962/health"* ]]; then
  if [ "${{1:-}}" != "-q" ]; then
    echo "gateway_health:missing_q:$1" >> "{events_trace}"
    exit 23
  fi
  hdr="$(cat)"
  if [ "$hdr" != "X-Service-Token: {_SECRET_TOKEN}" ]; then
    echo "gateway_health:bad_token" >> "{events_trace}"
    exit 22
  fi
  echo "gateway_health:token_ok" >> "{events_trace}"
{gw_health_snippet}
fi
if [[ "$*" == *"127.0.0.1:8000/health"* ]]; then
  echo "backend_health" >> "{events_trace}"
fi
echo 200
exit 0
"""
    )

    if git_diff_error_rc != 0 or git_rev_parse_gw_error_rc != 0:
        real_git = subprocess.check_output(["which", "git"], text=True).strip()
        (fake_bin / "git").write_text(
            f"""#!/bin/bash
if [ {git_diff_error_rc} -ne 0 ] && [ "${{1:-}}" = "diff" ] && [[ "$*" == *"integrations/dalton-channel-gateway/"* ]]; then
  echo "fatal: bad object in git diff" >&2
  exit {git_diff_error_rc}
fi
if [ {git_rev_parse_gw_error_rc} -ne 0 ] && [ "${{1:-}}" = "rev-parse" ] && [[ "$*" == *":integrations/dalton-channel-gateway"* ]]; then
  echo "fatal: bad tree object in git rev-parse" >&2
  exit {git_rev_parse_gw_error_rc}
fi
exec "{real_git}" "$@"
"""
        )
        (fake_bin / "git").chmod(0o755)

    for name in ("launchctl", "docker", "uv", "pnpm", "node", "curl"):
        (fake_bin / name).chmod(0o755)
    (fake_bin / "python3").symlink_to(_REPO_ROOT / ".venv/bin/python")

    return {
        "origin": origin,
        "seed": seed,
        "repo": repo,
        "gw_dir": gw_dir,
        "v1": v1,
        "v2": v2,
        "v1_gw_tree": v1_gw_tree,
        "v2_gw_tree": v2_gw_tree,
        "fake_bin": fake_bin,
        "backup_log": backup_log,
        "wait_idle": wait_idle,
        "events_trace": events_trace,
        "env_file": env_file,
    }


def _run_deploy(
    ctx: dict[str, Path | str],
    tmp_path: Path,
    *,
    args: str = "--reuse-backup-minutes 60",
    gateway_timeout: str = "2",
    gateway_poll: str = "1",
    extra_setup: str = "",
) -> subprocess.CompletedProcess[str]:
    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
WAIT_IDLE_SCRIPT="{ctx['wait_idle']}"
{extra_setup}
run_deploy_main {args}
"""
    return subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{ctx['fake_bin']}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(ctx["repo"]),
            "DALTON_PG_BACKUP_LOG": str(ctx["backup_log"]),
            "DALTON_ALEMBIC_QUERY_RETRY_DELAY_S": "0",
            "DALTON_GATEWAY_WAIT_TIMEOUT": gateway_timeout,
            "DALTON_GATEWAY_POLL_INTERVAL": gateway_poll,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_usage_gitignore_and_parse_args_support_gateway_flag() -> None:
    res = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; '
                'parse_deploy_args --gateway; echo "GW=$GATEWAY"; usage'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert "GW=1" in res.stdout
    assert "--gateway" in res.stdout
    assert "integrations/dalton-channel-gateway/" in res.stdout
    assert ".deploy-releases/" in res.stdout
    assert ".deploy-current" in res.stdout
    assert ".deploy-deployed-tree" in res.stdout

    header = "\n".join(_DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines()[:25])
    assert "--gateway" in header
    assert ".deploy-releases/" in header
    assert ".deploy-current" in header
    assert ".deploy-deployed-tree" in header

    gitignore_lines = _GATEWAY_GITIGNORE.read_text(encoding="utf-8").splitlines()
    for required in (
        "node_modules",
        "node_modules/",
        "dist",
        "dist/",
        ".deploy-releases/",
        ".deploy-current",
        ".deploy-deployed-tree",
        ".deploy-legacy-backup-*",
        ".deploy-failed.*",
        ".deploy-gateway-pending",
        ".deploy-gateway-pending.*",
    ):
        assert required in gitignore_lines


def test_gateway_health_body_ok_validates_ready_and_ok_strictly(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(_REPO_ROOT / ".venv/bin/python")

    def _check(body: str) -> int:
        res = subprocess.run(
            [
                "bash",
                "-c",
                (
                    f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; '
                    '_gateway_health_body_ok "$1"'
                ),
                "--",
                body,
            ],
            env={
                "PATH": f"{fake_bin}:/bin:/usr/bin",
                "HOME": str(tmp_path),
                "DALTON_REPO_ROOT": str(_REPO_ROOT),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode

    assert _check('{"ok":true,"ready":true}') == 0
    assert _check('{"ok":true}') == 0
    assert _check('{"ok":true,"ready":false}') != 0
    assert _check('{"ok":true,"ready":null}') != 0
    assert _check('{"ok":true,"ready":"true"}') != 0
    assert _check('{"ok":true,"ready":1}') != 0
    assert _check('{"ok":false}') != 0
    assert _check('{"ok":1}') != 0
    assert _check("{}") != 0
    assert _check("not json") != 0


def test_no_gateway_change_leaves_staging_and_production_untouched(tmp_path: Path) -> None:
    ctx = _setup_gateway_deploy_env(tmp_path, gateway_changed=False)
    res = _run_deploy(ctx, tmp_path)
    assert res.returncode == 0, res.stdout + res.stderr

    events = Path(ctx["events_trace"]).read_text().splitlines()
    assert not any("integrations/dalton-channel-gateway" in e and e.startswith("pnpm:") for e in events)
    assert not any(e.startswith("node_") for e in events)
    assert not any(e.startswith("launchctl:") for e in events)
    assert not any(e.startswith("gateway_health:") for e in events)

    gw_dir = Path(ctx["gw_dir"])
    assert (gw_dir / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert not (gw_dir / ".deploy-releases").exists()
    assert not (gw_dir / ".deploy-current").exists()
    assert _SECRET_TOKEN not in res.stdout
    assert _SECRET_TOKEN not in res.stderr


def test_gateway_change_migrates_legacy_dirs_to_atomic_symlinks_and_prunes_older_releases(
    tmp_path: Path,
) -> None:
    ctx = _setup_gateway_deploy_env(
        tmp_path,
        gateway_changed=True,
        gateway_health_mode="ready_true",
        stale_staging=True,
        stale_prev=True,
    )
    gw_dir = Path(ctx["gw_dir"])
    assert not (gw_dir / "dist").is_symlink()
    assert not (gw_dir / "node_modules").is_symlink()

    # Verify during apply_backend that production dist/node_modules are still v1
    prod_seen_during_backend = tmp_path / "prod_during_backend.txt"
    extra_setup = f"""
orig_apply_backend=$(declare -f apply_backend)
eval "real_${{orig_apply_backend}}"
apply_backend() {{
  printf '%s|%s\\n' "$(cat "{gw_dir}/dist/index.mjs")" "$(cat "{gw_dir}/node_modules/@larksuiteoapi/node-sdk/index.js")" > "{prod_seen_during_backend}"
  real_apply_backend "$@"
}}
"""
    res = _run_deploy(ctx, tmp_path, extra_setup=extra_setup)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "gateway health ok" in res.stdout

    # During backend restart, production gateway was still v1 (not yet switched)
    assert prod_seen_during_backend.read_text().strip() == "v1-bundle|v1-sdk"

    events = Path(ctx["events_trace"]).read_text().splitlines()
    uid = os.getuid()
    fe_idx = events.index("frontend_build")
    gw_pnpm_idx = next(
        i
        for i, e in enumerate(events)
        if e.startswith("pnpm:")
        and ".deploy-releases/rel-" in e
        and "install --frozen-lockfile" in e
    )
    gw_node_build_idx = next(
        i
        for i, e in enumerate(events)
        if e.startswith("node_build:")
        and ".deploy-releases/rel-" in e
        and "build.mjs" in e
    )
    backend_restart_idx = next(
        i for i, e in enumerate(events) if e == "docker:restart dalton-langalpha-backend-1"
    )
    backend_health_idx = next(i for i, e in enumerate(events) if e == "backend_health")
    gw_release_import_idx = next(
        i
        for i, e in enumerate(events)
        if e.startswith("node_import:") and ".deploy-releases/rel-" in e
    )
    gw_prod_import_idx = next(
        i
        for i, e in enumerate(events)
        if e.startswith("node_import:") and e.endswith("integrations/dalton-channel-gateway")
    )
    gw_kickstart_idx = next(
        i
        for i, e in enumerate(events)
        if e == f"launchctl:kickstart -k gui/{uid}/com.dalton.channel-gateway"
    )
    gw_health_idx = next(i for i, e in enumerate(events) if e == "gateway_health:token_ok")

    assert (
        fe_idx
        < gw_pnpm_idx
        < gw_node_build_idx
        < backend_restart_idx
        < backend_health_idx
        < gw_release_import_idx
        < gw_prod_import_idx
        < gw_kickstart_idx
        < gw_health_idx
    )
    # Build never ran pnpm/node build.mjs directly in production gateway root
    assert not any(
        e.startswith("pnpm:") and e.endswith("integrations/dalton-channel-gateway:install --frozen-lockfile")
        for e in events
    )
    assert not any(e.startswith("node_build:") and ".deploy-releases/" not in e for e in events)

    # First-time migration converted dist and node_modules to relative symlinks via .deploy-current
    assert (gw_dir / "dist").is_symlink()
    assert os.readlink(gw_dir / "dist") == ".deploy-current/dist"
    assert (gw_dir / "node_modules").is_symlink()
    assert os.readlink(gw_dir / "node_modules") == ".deploy-current/node_modules"
    assert (gw_dir / ".deploy-current").is_symlink()
    cur_rel_v2 = os.readlink(gw_dir / ".deploy-current")
    assert cur_rel_v2.startswith(".deploy-releases/rel-")

    assert (gw_dir / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v2-sdk\n"
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == str(ctx["v2_gw_tree"])

    releases_after_v2 = sorted(p.name for p in (gw_dir / ".deploy-releases").iterdir())
    assert len(releases_after_v2) == 2
    prev_dirs = [r for r in releases_after_v2 if r.startswith("prev-")]
    rel_dirs = [r for r in releases_after_v2 if r.startswith("rel-")]
    assert len(prev_dirs) == 1 and len(rel_dirs) == 1
    assert (gw_dir / ".deploy-releases" / prev_dirs[0] / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (
        gw_dir / ".deploy-releases" / prev_dirs[0] / "node_modules/@larksuiteoapi/node-sdk/index.js"
    ).read_text() == "v1-sdk\n"
    assert not list(gw_dir.glob(".deploy-legacy-backup-*"))
    assert not (gw_dir / ".deploy-staging").exists()
    assert not (gw_dir / ".deploy-prev").exists()

    # Now push v3 and deploy again: already in symlink mode -> single atomic .deploy-current switch,
    # and prune_old_gateway_releases keeps only v3 (current) and v2 (previous), deleting v1 (prev-*).
    seed = Path(ctx["seed"])
    (seed / "integrations/dalton-channel-gateway/src/main.ts").write_text("export const v = 3;\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v3"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    v3_gw_tree = subprocess.check_output(
        ["git", "-C", str(seed), "rev-parse", "HEAD:integrations/dalton-channel-gateway"],
        text=True,
    ).strip()

    res_v3 = _run_deploy(ctx, tmp_path)
    assert res_v3.returncode == 0, res_v3.stdout + res_v3.stderr
    assert (gw_dir / "dist/index.mjs").read_text() == "v3-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v3-sdk\n"
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == v3_gw_tree
    releases_after_v3 = sorted(p.name for p in (gw_dir / ".deploy-releases").iterdir())
    assert len(releases_after_v3) == 2
    assert prev_dirs[0] not in releases_after_v3
    assert rel_dirs[0] in releases_after_v3
    assert _SECRET_TOKEN not in res.stdout
    assert _SECRET_TOKEN not in res.stderr


def test_gateway_flag_forces_deploy_without_diff_and_accepts_ok_without_ready(
    tmp_path: Path,
) -> None:
    ctx = _setup_gateway_deploy_env(
        tmp_path,
        gateway_changed=False,
        gateway_health_mode="ok_only",
    )
    res = _run_deploy(ctx, tmp_path, args="--gateway --reuse-backup-minutes 60")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "gateway health ok" in res.stdout

    events = Path(ctx["events_trace"]).read_text().splitlines()
    uid = os.getuid()
    assert any(".deploy-releases/rel-" in e and e.startswith("pnpm:") for e in events)
    assert any(".deploy-releases/rel-" in e and e.startswith("node_build:") for e in events)
    assert any(e.startswith("node_import:") for e in events)
    assert f"launchctl:kickstart -k gui/{uid}/com.dalton.channel-gateway" in events
    assert "gateway_health:token_ok" in events
    gw_dir = Path(ctx["gw_dir"])
    assert (gw_dir / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == str(ctx["v2_gw_tree"])
    assert _SECRET_TOKEN not in res.stdout
    assert _SECRET_TOKEN not in res.stderr


def test_gateway_build_failure_leaves_prod_untouched_cleans_staging_and_never_says_all_passed(
    tmp_path: Path,
) -> None:
    # Case 1: node build.mjs fails in staging release dir
    case1 = tmp_path / "node_fail"
    case1.mkdir()
    ctx1 = _setup_gateway_deploy_env(
        case1,
        gateway_changed=True,
        gateway_node_build_behavior="fail",
    )
    res1 = _run_deploy(ctx1, case1)
    assert res1.returncode != 0
    gw_dir1 = Path(ctx1["gw_dir"])
    assert (gw_dir1 / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir1 / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert not list((gw_dir1 / ".deploy-releases").glob("rel-*"))
    assert not (gw_dir1 / ".deploy-current").exists()
    assert (
        subprocess.check_output(["git", "-C", str(ctx1["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx1["v1"])
    )
    events1 = Path(ctx1["events_trace"]).read_text().splitlines()
    assert not any("restart dalton-langalpha-backend-1" in e for e in events1)
    assert not any("compose up" in e for e in events1)
    assert not any("com.dalton.channel-gateway" in e for e in events1)
    assert "网关未切换" in res1.stdout
    assert "all revert steps passed" not in res1.stdout

    # Case 2: pnpm install fails in staging release dir
    case2 = tmp_path / "pnpm_fail"
    case2.mkdir()
    ctx2 = _setup_gateway_deploy_env(
        case2,
        gateway_changed=True,
        gateway_pnpm_rc=1,
    )
    res2 = _run_deploy(ctx2, case2)
    assert res2.returncode != 0
    gw_dir2 = Path(ctx2["gw_dir"])
    assert (gw_dir2 / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir2 / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert not list((gw_dir2 / ".deploy-releases").glob("rel-*"))
    assert (
        subprocess.check_output(["git", "-C", str(ctx2["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx2["v1"])
    )
    assert "网关未切换" in res2.stdout
    assert "all revert steps passed" not in res2.stdout


def test_migrating_deploy_aborts_before_backend_restart_leaves_prod_untouched_and_cleans_staging(
    tmp_path: Path,
) -> None:
    ctx = _setup_gateway_deploy_env(
        tmp_path,
        gateway_changed=True,
        with_migration_change=True,
        wait_idle_fail_after_first=True,
    )
    res = _run_deploy(ctx, tmp_path, args="")
    assert res.returncode != 0

    gw_dir = Path(ctx["gw_dir"])
    assert (gw_dir / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert not list((gw_dir / ".deploy-releases").glob("rel-*"))
    assert not (gw_dir / ".deploy-current").exists()
    # Pre-deploy tree hash was recorded as v1 so next deploy knows gateway is still on v1
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == str(ctx["v1_gw_tree"])

    events = Path(ctx["events_trace"]).read_text().splitlines()
    assert any(".deploy-releases/rel-" in e and e.startswith("node_build:") for e in events)
    assert not any("restart dalton-langalpha-backend-1" in e for e in events)
    assert not any("com.dalton.channel-gateway" in e for e in events)
    assert "AUTO-REVERT ABORTED" in res.stdout
    assert "gateway_switched=0" in res.stdout


def test_pre_switch_and_post_switch_import_check_failures(tmp_path: Path) -> None:
    # Case 1: Pre-switch release import fails -> zero switch, production untouched!
    case1 = tmp_path / "pre_switch_fail"
    case1.mkdir()
    ctx1 = _setup_gateway_deploy_env(
        case1,
        gateway_changed=True,
        gateway_staging_import_rc=1,
    )
    res1 = _run_deploy(ctx1, case1)
    assert res1.returncode != 0
    gw_dir1 = Path(ctx1["gw_dir"])
    assert not (gw_dir1 / "dist").is_symlink()
    assert (gw_dir1 / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir1 / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert not list((gw_dir1 / ".deploy-releases").glob("rel-*"))
    assert "gateway_switched=0" in res1.stdout
    assert "文件换回: 跳过（切换未完成，生产目录保持上一版完整状态）" in res1.stdout
    assert "磁盘当前版本: 完整（dist/index.mjs 与 node_modules 均存在；.deploy-current=未设置；布局: 真实目录 dist 与 node_modules" in res1.stdout
    assert f"已知树哈希: {ctx1['v1_gw_tree']}" in res1.stdout

    # Case 2: Post-switch production import fails -> restores previous release via .deploy-current without kickstart
    case2 = tmp_path / "post_switch_fail"
    case2.mkdir()
    ctx2 = _setup_gateway_deploy_env(
        case2,
        gateway_changed=True,
        gateway_import_rc=1,
    )
    res2 = _run_deploy(ctx2, case2)
    assert res2.returncode != 0

    gw_dir2 = Path(ctx2["gw_dir"])
    assert (gw_dir2 / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir2 / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert (gw_dir2 / ".deploy-deployed-tree").read_text().strip() == str(ctx2["v1_gw_tree"])
    assert (gw_dir2 / ".deploy-gateway-pending").exists()

    events2 = Path(ctx2["events_trace"]).read_text().splitlines()
    assert not any("com.dalton.channel-gateway" in e for e in events2)
    assert [e for e in events2 if e == "docker:restart dalton-langalpha-backend-1"] == [
        "docker:restart dalton-langalpha-backend-1"
    ]
    assert (
        subprocess.check_output(["git", "-C", str(ctx2["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx2["v2"])
    )

    out2 = res2.stdout
    assert "GATEWAY ROLLBACK" in out2
    assert "gateway_switched=1" in out2
    assert "文件换回: 成功" in out2
    assert "进程重启: 跳过" in out2
    assert "健康复查: 跳过" in out2
    assert "磁盘当前版本: 完整（dist/index.mjs 与 node_modules 均存在；.deploy-current=.deploy-releases/prev-" in out2
    assert f"已知树哈希: {ctx2['v1_gw_tree']}" in out2
    assert "可能存在接口不匹配" in out2


def test_first_kickstart_failure_restores_prev_then_restarts_and_checks_health(
    tmp_path: Path,
) -> None:
    ctx = _setup_gateway_deploy_env(
        tmp_path,
        gateway_changed=True,
        gateway_kickstart_mode="fail_first",
        gateway_health_mode="ready_false_until_restored",
    )
    res = _run_deploy(ctx, tmp_path)
    assert res.returncode != 0

    uid = os.getuid()
    events = Path(ctx["events_trace"]).read_text().splitlines()
    gw_kickstarts = [
        e for e in events if e == f"launchctl:kickstart -k gui/{uid}/com.dalton.channel-gateway"
    ]
    assert len(gw_kickstarts) == 2

    gw_dir = Path(ctx["gw_dir"])
    assert (gw_dir / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == str(ctx["v1_gw_tree"])
    assert (
        subprocess.check_output(["git", "-C", str(ctx["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx["v2"])
    )

    out = res.stdout
    assert "文件换回: 成功" in out
    assert "进程重启: 成功" in out
    assert "健康复查: 成功" in out
    assert "磁盘当前版本: 完整（dist/index.mjs 与 node_modules 均存在；.deploy-current=.deploy-releases/prev-" in out
    assert f"已知树哈希: {ctx['v1_gw_tree']}" in out
    assert _SECRET_TOKEN not in res.stdout
    assert _SECRET_TOKEN not in res.stderr


def test_gateway_health_timeout_rolls_back_gateway_only_and_keeps_new_backend(
    tmp_path: Path,
) -> None:
    ctx = _setup_gateway_deploy_env(
        tmp_path,
        gateway_changed=True,
        gateway_health_mode="ready_false_until_restored",
    )
    res = _run_deploy(ctx, tmp_path, gateway_timeout="1", gateway_poll="1")
    assert res.returncode != 0

    uid = os.getuid()
    events = Path(ctx["events_trace"]).read_text().splitlines()
    gw_kickstarts = [
        e for e in events if e == f"launchctl:kickstart -k gui/{uid}/com.dalton.channel-gateway"
    ]
    backend_restarts = [e for e in events if e == "docker:restart dalton-langalpha-backend-1"]
    assert len(gw_kickstarts) == 2
    assert len(backend_restarts) == 1

    gw_dir = Path(ctx["gw_dir"])
    assert (gw_dir / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == str(ctx["v1_gw_tree"])
    assert (
        subprocess.check_output(["git", "-C", str(ctx["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx["v2"])
    )

    out = res.stdout
    assert "GATEWAY ROLLBACK" in out
    assert f"HEAD={ctx['v2']}" in out
    assert "gateway_switched=1" in out
    assert "文件换回: 成功" in out
    assert "进程重启: 成功" in out
    assert "健康复查: 成功" in out
    assert "磁盘当前版本: 完整（dist/index.mjs 与 node_modules 均存在；.deploy-current=.deploy-releases/prev-" in out
    assert f"已知树哈希: {ctx['v1_gw_tree']}" in out
    assert "后端已是新版" in out
    assert "可能存在接口不匹配" in out
    assert "all revert steps passed" not in out
    assert _SECRET_TOKEN not in res.stdout
    assert _SECRET_TOKEN not in res.stderr


@pytest.mark.skipif(
    not (sys.platform == "darwin" or sys.platform.startswith("linux")),
    reason="requires macOS RENAME_SWAP (renamex_np) or Linux RENAME_EXCHANGE (renameat2)",
)
def test_switch_and_restore_failures_preserve_recoverable_dirs_and_report_honest_disk_state(
    tmp_path: Path,
) -> None:
    # Case 1: switch_gateway_from_staging fails during legacy migration after swapping dist to .deploy-legacy-backup-*,
    # and swapping dist back ALSO fails (leaving dist broken/missing) -> must preserve .deploy-legacy-backup-* and .deploy-releases/*,
    # and must report incomplete disk state (missing dist) rather than lying "原生产目录未改动"!
    case1 = tmp_path / "switch_double_fail"
    case1.mkdir()
    ctx1 = _setup_gateway_deploy_env(case1, gateway_changed=True)
    swap_calls1 = case1 / "swap_calls"
    swap_calls1.write_text("0")
    extra_setup1 = f"""
orig_swap=$(declare -f _atomic_swap_paths)
eval "real_${{orig_swap}}"
_atomic_swap_paths() {{
  local n=$(( $(cat "{swap_calls1}") + 1 ))
  echo "$n" > "{swap_calls1}"
  if [ "$n" -eq 1 ]; then
    real__atomic_swap_paths "$@"
    return $?
  fi
  if [ "$n" -eq 2 ]; then
    # Fail node_modules swap
    return 1
  fi
  # 3rd call is rollback swap of dist: simulate double failure that leaves dist symlink broken/removed
  rm -f "$2"
  return 1
}}
"""
    res1 = _run_deploy(ctx1, case1, extra_setup=extra_setup1)
    assert res1.returncode != 0
    gw_dir1 = Path(ctx1["gw_dir"])
    legacy_backups1 = list(gw_dir1.glob(".deploy-legacy-backup-*"))
    assert len(legacy_backups1) == 1
    assert (legacy_backups1[0] / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert list((gw_dir1 / ".deploy-releases").glob("prev-*"))
    assert list((gw_dir1 / ".deploy-releases").glob("rel-*"))
    assert "原生产目录未改动" not in res1.stdout
    assert "文件换回: 失败（切换中途失败且内部恢复未完成，已保留构建与备份目录）" in res1.stdout
    assert "磁盘当前版本: 不完整（缺失: dist" in res1.stdout
    assert ".deploy-legacy-backup-" in res1.stdout

    # Case 2: rollback_gateway_only when restore_gateway_from_prev fails AND leaves dist missing
    # (simulated double failure in real-dir restore path where moving from .deploy-failed.* back also fails)
    # -> must preserve .deploy-failed.*, skip second kickstart, and honestly report missing dist & recovery path!
    case2 = tmp_path / "restore_double_fail"
    case2.mkdir()
    ctx2 = _setup_gateway_deploy_env(
        case2,
        gateway_changed=True,
        gateway_health_mode="always_ready_false",
    )
    gw_dir2 = Path(ctx2["gw_dir"])
    extra_setup2 = f"""
orig_restore=$(declare -f restore_gateway_from_prev)
eval "real_${{orig_restore}}"
restore_gateway_from_prev() {{
  # Simulate real-directory state before restore and double failure when restoring from .deploy-failed.$$
  rm -f "{gw_dir2}/dist" "{gw_dir2}/node_modules"
  mkdir -p "{gw_dir2}/dist" "{gw_dir2}/node_modules"
  echo "v2-bundle" > "{gw_dir2}/dist/index.mjs"
  mv() {{
    if [[ "${{1:-}}" == *"/node_modules" ]] && [[ "${{2:-}}" == *".deploy-failed."* ]]; then
      return 1
    fi
    if [[ "${{1:-}}" == *".deploy-failed."*"/dist" ]]; then
      return 1
    fi
    command mv "$@"
  }}
  real_restore_gateway_from_prev
  local rc=$?
  unset -f mv
  return "$rc"
}}
"""
    res2 = _run_deploy(
        ctx2,
        case2,
        gateway_timeout="1",
        gateway_poll="1",
        extra_setup=extra_setup2,
    )
    assert res2.returncode != 0
    uid = os.getuid()
    events2 = Path(ctx2["events_trace"]).read_text().splitlines()
    gw_kickstarts2 = [
        e for e in events2 if e == f"launchctl:kickstart -k gui/{uid}/com.dalton.channel-gateway"
    ]
    assert len(gw_kickstarts2) == 1
    failed_dirs2 = list(gw_dir2.glob(".deploy-failed.*"))
    assert len(failed_dirs2) == 1
    assert (failed_dirs2[0] / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert "文件换回: 失败" in res2.stdout
    assert "进程重启: 跳过" in res2.stdout
    assert "健康复查: 跳过" in res2.stdout
    assert "磁盘当前版本: 不完整（缺失: dist" in res2.stdout
    assert ".deploy-failed." in res2.stdout
    assert "新版产物仍在生产目录" not in res2.stdout

    # Case 3: revert_deploy when GATEWAY_SWITCHED=1 and no previous release exists -> NO kickstart, FAILED report
    case3 = tmp_path / "revert_restore_fail"
    case3.mkdir()
    ctx3 = _setup_gateway_deploy_env(case3, gateway_changed=True)
    repo3 = Path(ctx3["repo"])
    subprocess.run(["git", "-C", str(repo3), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(repo3), "merge", "-q", "--ff-only", str(ctx3["v2"])], check=True)

    trace3 = case3 / "revert.trace"
    script3 = f"""
set -uo pipefail
export PATH="{ctx3['fake_bin']}:/bin:/usr/bin"
DALTON_DEPLOY_LIB_ONLY=1
DALTON_REPO_ROOT="{repo3}"
source "{_DEPLOY_SCRIPT}"
cd "{repo3}"
PRE_FF="{ctx3['v1']}"
PRE_FF_DALTON_REV="d040"
PRE_FF_UPSTREAM_REV="146"
MIGRATING_DEPLOY="0"
BACKEND_TOUCHED="0"
CRAWLER_TOUCHED="0"
EDGE_TOUCHED="0"
BRIDGE_TOUCHED="0"
GATEWAY_SWITCHED="1"
launchctl() {{ echo "launchctl:$*" >> "{trace3}"; return 0; }}
revert_deploy
"""
    res3 = subprocess.run(["bash", "-c", script3], capture_output=True, text=True, check=False)
    assert res3.returncode != 0
    assert not trace3.exists()
    assert "POST-REVERT REPORT: FAILED" in res3.stdout
    assert "gateway_restore" in res3.stdout
    assert "all revert steps passed" not in res3.stdout
    assert "网关人工恢复步骤" in res3.stdout
    assert "rm -rf integrations/dalton-channel-gateway/dist" not in res3.stdout


def test_redeploy_after_migrating_wait_idle_abort_or_gateway_rollback_deploys_gateway_via_deployed_tree(
    tmp_path: Path,
) -> None:
    # Scenario A: Migrating deploy aborts at 2nd wait_idle (HEAD=v2, gateway still v1).
    # Then a subsequent commit v3 only touches app.py (PRE_FF..TARGET gateway diff is empty!).
    # Normal deploy (without --gateway) must still deploy gateway because .deploy-deployed-tree != target_gw_tree!
    case_a = tmp_path / "after_migrating_abort"
    case_a.mkdir()
    ctx_a = _setup_gateway_deploy_env(
        case_a,
        gateway_changed=True,
        with_migration_change=True,
        wait_idle_fail_after_first=True,
    )
    res_a1 = _run_deploy(ctx_a, case_a, args="")
    assert res_a1.returncode != 0
    gw_dir_a = Path(ctx_a["gw_dir"])
    assert (gw_dir_a / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir_a / ".deploy-deployed-tree").read_text().strip() == str(ctx_a["v1_gw_tree"])

    # Push v3 that modifies ONLY app.py (zero gateway diff between v2 and v3), and let idle wait pass
    seed_a = Path(ctx_a["seed"])
    (seed_a / "app.py").write_text("v3-no-gw-diff\n")
    subprocess.run(["git", "-C", str(seed_a), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(seed_a), "commit", "-q", "-m", "v3 app only"], check=True)
    subprocess.run(["git", "-C", str(seed_a), "push", "-q", "origin", "dalton/main"], check=True)

    Path(ctx_a["wait_idle"]).write_text("#!/bin/bash\nexit 0\n")
    docker_a = Path(ctx_a["fake_bin"]) / "docker"
    docker_a.write_text(docker_a.read_text().replace('echo "1"', 'echo "0"'))

    res_a2 = _run_deploy(ctx_a, case_a, args="")
    assert res_a2.returncode == 0, res_a2.stdout + res_a2.stderr
    assert "gateway health ok" in res_a2.stdout
    assert (gw_dir_a / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert (gw_dir_a / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v2-sdk\n"
    assert (gw_dir_a / ".deploy-deployed-tree").read_text().strip() == str(ctx_a["v2_gw_tree"])

    # Scenario B: Gateway health check fails -> rollback_gateway_only leaves HEAD=v2 and gateway=v1.
    # Re-running normal deploy on the exact same commit v2 (PRE_FF == TARGET == v2, without --gateway)
    # after gateway becomes healthy must deploy v2 gateway and update .deploy-deployed-tree!
    case_b = tmp_path / "after_gateway_rollback"
    case_b.mkdir()
    ctx_b = _setup_gateway_deploy_env(
        case_b,
        gateway_changed=True,
        gateway_health_mode="ready_false_until_restored",
    )
    res_b1 = _run_deploy(ctx_b, case_b, gateway_timeout="1", gateway_poll="1")
    assert res_b1.returncode != 0
    gw_dir_b = Path(ctx_b["gw_dir"])
    assert (gw_dir_b / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir_b / ".deploy-deployed-tree").read_text().strip() == str(ctx_b["v1_gw_tree"])
    assert (
        subprocess.check_output(["git", "-C", str(ctx_b["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx_b["v2"])
    )

    # Now gateway health recovers; re-run normal deploy WITHOUT --gateway on same commit v2
    curl_b = Path(ctx_b["fake_bin"]) / "curl"
    curl_b.write_text(
        """#!/bin/bash
if [[ "$*" == *"127.0.0.1:8962/health"* ]]; then
  cat >/dev/null
  echo '{"ok":true,"ready":true}'
  exit 0
fi
echo 200
exit 0
"""
    )
    curl_b.chmod(0o755)

    res_b2 = _run_deploy(ctx_b, case_b)
    assert res_b2.returncode == 0, res_b2.stdout + res_b2.stderr
    assert "gateway health ok" in res_b2.stdout
    assert (gw_dir_b / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert (gw_dir_b / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v2-sdk\n"
    assert (gw_dir_b / ".deploy-deployed-tree").read_text().strip() == str(ctx_b["v2_gw_tree"])


def test_revert_deploy_restores_prev_and_never_outputs_destructive_rm_rf(
    tmp_path: Path,
) -> None:
    # Case 1: MIGRATING_DEPLOY=0 + GATEWAY_SWITCHED=1 -> atomically switches .deploy-current back, kickstarts, and checks gateway health
    case1 = tmp_path / "auto_revert"
    case1.mkdir()
    ctx1 = _setup_gateway_deploy_env(case1, gateway_changed=True)
    repo1 = Path(ctx1["repo"])
    gw_dir1 = Path(ctx1["gw_dir"])
    subprocess.run(["git", "-C", str(repo1), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(repo1), "merge", "-q", "--ff-only", str(ctx1["v2"])], check=True)

    rm_v1_dir = gw_dir1 / ".deploy-releases/prev-1"
    rm_v2_dir = gw_dir1 / ".deploy-releases/rel-2"
    (rm_v1_dir / "dist").mkdir(parents=True)
    (rm_v1_dir / "dist/index.mjs").write_text("v1-bundle\n")
    (rm_v1_dir / "node_modules/@larksuiteoapi/node-sdk").mkdir(parents=True)
    (rm_v1_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").write_text("v1-sdk\n")
    (rm_v1_dir / ".deploy-tree").write_text(f"{ctx1['v1_gw_tree']}\n")
    (rm_v2_dir / "dist").mkdir(parents=True)
    (rm_v2_dir / "dist/index.mjs").write_text("v2-bundle\n")
    (rm_v2_dir / "node_modules/@larksuiteoapi/node-sdk").mkdir(parents=True)
    (rm_v2_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").write_text("v2-sdk\n")
    (rm_v2_dir / ".deploy-tree").write_text(f"{ctx1['v2_gw_tree']}\n")
    subprocess.run(["rm", "-rf", str(gw_dir1 / "dist"), str(gw_dir1 / "node_modules")], check=True)
    (gw_dir1 / ".deploy-current").symlink_to(".deploy-releases/rel-2")
    (gw_dir1 / "dist").symlink_to(".deploy-current/dist")
    (gw_dir1 / "node_modules").symlink_to(".deploy-current/node_modules")

    trace1 = case1 / "revert.trace"
    uid = os.getuid()
    script1 = f"""
set -uo pipefail
export PATH="{ctx1['fake_bin']}:/bin:/usr/bin"
export HOME="{case1}"
DALTON_DEPLOY_LIB_ONLY=1
DALTON_REPO_ROOT="{repo1}"
source "{_DEPLOY_SCRIPT}"
cd "{repo1}"
PRE_FF="{ctx1['v1']}"
PRE_FF_DALTON_REV="d040"
PRE_FF_UPSTREAM_REV="146"
MIGRATING_DEPLOY="0"
BACKEND_TOUCHED="0"
CRAWLER_TOUCHED="0"
EDGE_TOUCHED="0"
BRIDGE_TOUCHED="0"
GATEWAY_SWITCHED="1"
PREV_GATEWAY_RELEASE_REL=".deploy-releases/prev-1"
launchctl() {{ echo "launchctl:$*" >> "{trace1}"; return 0; }}
revert_deploy
"""
    res1 = subprocess.run(["bash", "-c", script1], capture_output=True, text=True, check=False)
    assert res1.returncode == 0, res1.stdout + res1.stderr
    assert "POST-REVERT CHECK: gateway health OK" in res1.stdout
    assert (gw_dir1 / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir1 / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert os.readlink(gw_dir1 / ".deploy-current") == ".deploy-releases/prev-1"
    assert (gw_dir1 / ".deploy-deployed-tree").read_text().strip() == str(ctx1["v1_gw_tree"])
    assert (
        f"launchctl:kickstart -k gui/{uid}/com.dalton.channel-gateway"
        in trace1.read_text().splitlines()
    )

    # Case 2: Full migrating deploy where gateway switches and passes health, then crawler health fails ->
    # enters revert_deploy, automatically switches gateway back to v1, verifies gateway health, and manual instructions step 8
    # MUST NOT contain `rm -rf integrations/dalton-channel-gateway/dist` or in-place `node build.mjs`!
    case2 = tmp_path / "migrating_crawler_fail"
    case2.mkdir()
    ctx2 = _setup_gateway_deploy_env(
        case2,
        gateway_changed=True,
        with_migration_change=True,
        crawler_health_fail=True,
    )
    res2 = _run_deploy(
        ctx2,
        case2,
        args="",
        extra_setup='wait_crawler_health() { echo "crawler health: unreachable"; return 1; }',
    )
    assert res2.returncode != 0
    gw_dir2 = Path(ctx2["gw_dir"])
    assert (gw_dir2 / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir2 / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert (gw_dir2 / ".deploy-deployed-tree").read_text().strip() == str(ctx2["v1_gw_tree"])
    assert (gw_dir2 / ".deploy-gateway-pending").exists()

    out2 = res2.stdout
    assert "POST-REVERT CHECK: gateway health OK" in out2
    assert "AUTO-REVERT ABORTED" in out2
    assert "渠道网关已在回退阶段自动换回至上一版并重启且健康检查通过" in out2
    assert "gateway_switched=1, gateway_revert_state=reverted, gateway_revert_health_ok=1" in out2
    assert f"渠道网关已在回退阶段自动换回至上一版 release（gateway_revert_state=reverted；实际恢复的网关树哈希={ctx2['v1_gw_tree']}；pending=是）" in out2
    assert "rm -rf integrations/dalton-channel-gateway/dist" not in out2
    assert "pnpm install --frozen-lockfile && node build.mjs" not in out2
    assert ".deploy-prev" not in out2
    assert (
        f'DALTON_REPO_ROOT="{ctx2["repo"]}" DALTON_DEPLOY_LIB_ONLY=1 bash -c '
        "'source /tmp/dalton_deploy_main.sh && manual_rebuild_gateway_release'"
    ) in out2
    reset_idx = out2.index('git reset --keep "$RESET_SHA"')
    reverted_note_idx = out2.index("渠道网关已在回退阶段自动换回至上一版 release")
    rebuild_idx = out2.index("manual_rebuild_gateway_release")
    bootstrap_gw_idx = out2.index("com.dalton.channel-gateway.plist")
    assert reset_idx < reverted_note_idx < rebuild_idx < bootstrap_gw_idx


def test_sigterm_cleans_up_unswitched_staging_release_and_leaves_production_untouched(
    tmp_path: Path,
) -> None:
    ctx = _setup_gateway_deploy_env(tmp_path, gateway_changed=True)
    gw_dir = Path(ctx["gw_dir"])
    ready_for_sigterm = tmp_path / "ready_for_sigterm"

    # Wait in the second wait_idle (right after build_gateway_staging completes) until SIGTERM arrives
    wait_idle_calls = tmp_path / "sig_wait_idle.calls"
    wait_idle_calls.write_text("0")
    Path(ctx["wait_idle"]).write_text(
        f"""#!/bin/bash
n=$(( $(cat "{wait_idle_calls}") + 1 ))
echo "$n" > "{wait_idle_calls}"
if [ "$n" -gt 1 ]; then
  touch "{ready_for_sigterm}"
  while :; do sleep 0.05; done
fi
exit 0
"""
    )
    Path(ctx["wait_idle"]).chmod(0o755)
    docker_bin = Path(ctx["fake_bin"]) / "docker"
    docker_bin.write_text(
        docker_bin.read_text().replace(
            'elif echo "$*" | grep -q "conversation_threads"; then\n      echo "0"',
            'elif echo "$*" | grep -q "conversation_threads"; then\n      echo "1"',
        )
    )

    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
WAIT_IDLE_SCRIPT="{ctx['wait_idle']}"
run_deploy_main --reuse-backup-minutes 60
"""
    proc = subprocess.Popen(
        ["bash", "-c", script],
        env={
            "PATH": f"{ctx['fake_bin']}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(ctx["repo"]),
            "DALTON_PG_BACKUP_LOG": str(ctx["backup_log"]),
            "DALTON_ALEMBIC_QUERY_RETRY_DELAY_S": "0",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.time() + 15
    while time.time() < deadline and not ready_for_sigterm.exists():
        time.sleep(0.05)
    assert ready_for_sigterm.exists(), "deploy script did not reach post-build wait_idle"
    # Verify the staging release directory was created before sending SIGTERM
    assert list((gw_dir / ".deploy-releases").glob("rel-*"))

    os.killpg(proc.pid, signal.SIGTERM)
    _stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode != 0
    assert "cleaned up unswitched gateway staging release" in stderr
    assert not list((gw_dir / ".deploy-releases").glob("rel-*"))
    assert (gw_dir / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"


def test_health_check_under_set_x_hides_token_passes_curl_q_and_restores_xtrace(
    tmp_path: Path,
) -> None:
    env_dir = tmp_path / ".config/dalton"
    env_dir.mkdir(parents=True)
    env_file = env_dir / "channel-gateway.env"
    env_file.write_text(f'INTERNAL_SERVICE_TOKEN="{_SECRET_TOKEN}"\n')
    env_file.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_args_log = tmp_path / "curl.args"
    (fake_bin / "curl").write_text(
        f"""#!/bin/bash
echo "$*" >> "{curl_args_log}"
hdr="$(cat)"
[ "${{1:-}}" = "-q" ] || exit 23
[ "$hdr" = "X-Service-Token: {_SECRET_TOKEN}" ] || exit 22
echo '{{"ok":true,"ready":true}}'
exit 0
"""
    )
    (fake_bin / "curl").chmod(0o755)
    (fake_bin / "python3").symlink_to(_REPO_ROOT / ".venv/bin/python")

    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
set -x
wait_gateway_health 5
rc=$?
case "$-" in
  *x*) echo "XTRACE_RESTORED=1" ;;
  *) echo "XTRACE_RESTORED=0" ;;
esac
exit "$rc"
"""
    res = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(_REPO_ROOT),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "gateway health ok" in res.stdout
    assert "XTRACE_RESTORED=1" in res.stdout
    assert _SECRET_TOKEN not in res.stdout
    assert _SECRET_TOKEN not in res.stderr
    curl_lines = curl_args_log.read_text().splitlines()
    assert curl_lines and curl_lines[0].startswith("-q ")


def test_wait_gateway_health_enforces_strict_lt_deadline_and_caps_max_time_at_5s(
    tmp_path: Path,
) -> None:
    env_dir = tmp_path / ".config/dalton"
    env_dir.mkdir(parents=True)
    env_file = env_dir / "channel-gateway.env"
    env_file.write_text(f'INTERNAL_SERVICE_TOKEN="{_SECRET_TOKEN}"\n')
    env_file.chmod(0o600)

    # Case 1: timeout=1, poll=2; attempt 1 fails fast, attempt 2 would return ok -> must NOT run attempt 2 after deadline
    bin1 = tmp_path / "bin1"
    bin1.mkdir()
    attempts1 = tmp_path / "attempts1"
    attempts1.write_text("0")
    args1 = tmp_path / "args1"
    (bin1 / "curl").write_text(
        f"""#!/bin/bash
echo "$*" >> "{args1}"
cat >/dev/null
n=$(( $(cat "{attempts1}") + 1 ))
echo "$n" > "{attempts1}"
if [ "$n" -eq 1 ]; then
  echo '{{"ok":true,"ready":false}}'
  exit 0
fi
echo '{{"ok":true,"ready":true}}'
exit 0
"""
    )
    (bin1 / "curl").chmod(0o755)
    (bin1 / "python3").symlink_to(_REPO_ROOT / ".venv/bin/python")

    res1 = subprocess.run(
        [
            "bash",
            "-c",
            f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; wait_gateway_health 1',
        ],
        env={
            "PATH": f"{bin1}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(_REPO_ROOT),
            "DALTON_GATEWAY_POLL_INTERVAL": "2",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode != 0
    assert int(attempts1.read_text().strip()) == 1
    assert "--max-time 1" in args1.read_text()

    # Case 2: Fixed clock where deadline=101 (start=100, timeout=1), pre-probe now=100,
    # and post-probe check reads 101 (now == deadline) -> strict -lt must reject and fail!
    bin2 = tmp_path / "bin2"
    bin2.mkdir()
    clock_ticks = tmp_path / "clock_ticks"
    clock_ticks.write_text("0")
    (bin2 / "date").write_text(
        f"""#!/bin/bash
if [ "${{1:-}}" = "+%s" ]; then
  n=$(( $(cat "{clock_ticks}") + 1 ))
  echo "$n" > "{clock_ticks}"
  # Tick 1 (deadline calc): 100 -> deadline = 101
  # Tick 2 (pre-probe check): 100 -> remaining = 1
  # Tick 3 (post-probe check): 101 -> now == deadline (101 < 101 is false)
  if [ "$n" -le 2 ]; then
    echo 100
  else
    echo 101
  fi
  exit 0
fi
exec /bin/date "$@"
"""
    )
    (bin2 / "curl").write_text(
        """#!/bin/bash
cat >/dev/null
echo '{"ok":true,"ready":true}'
exit 0
"""
    )
    (bin2 / "date").chmod(0o755)
    (bin2 / "curl").chmod(0o755)
    (bin2 / "python3").symlink_to(_REPO_ROOT / ".venv/bin/python")

    res2 = subprocess.run(
        [
            "bash",
            "-c",
            f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; wait_gateway_health 1',
        ],
        env={
            "PATH": f"{bin2}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(_REPO_ROOT),
            "DALTON_GATEWAY_POLL_INTERVAL": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode != 0
    assert "gateway health ok" not in res2.stdout
    assert "gateway health: failed" in res2.stdout

    # Case 3: timeout=20 -> --max-time is capped at 5
    bin3 = tmp_path / "bin3"
    bin3.mkdir()
    args3 = tmp_path / "args3"
    (bin3 / "curl").write_text(
        f"""#!/bin/bash
echo "$*" >> "{args3}"
cat >/dev/null
echo '{{"ok":true,"ready":true}}'
exit 0
"""
    )
    (bin3 / "curl").chmod(0o755)
    (bin3 / "python3").symlink_to(_REPO_ROOT / ".venv/bin/python")

    res3 = subprocess.run(
        [
            "bash",
            "-c",
            f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; wait_gateway_health 20',
        ],
        env={
            "PATH": f"{bin3}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(_REPO_ROOT),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res3.returncode == 0, res3.stdout + res3.stderr
    assert "--max-time 5" in args3.read_text()


def test_git_diff_and_rev_parse_errors_abort_before_build_and_revert(tmp_path: Path) -> None:
    # Case 1: git diff returns 128
    case1 = tmp_path / "diff_err"
    case1.mkdir()
    ctx1 = _setup_gateway_deploy_env(
        case1,
        gateway_changed=False,
        git_diff_error_rc=128,
    )
    res1 = _run_deploy(ctx1, case1)
    assert res1.returncode != 0
    assert "git diff failed while checking integrations/dalton-channel-gateway/ (exit 128)" in res1.stderr

    events1 = Path(ctx1["events_trace"]).read_text().splitlines()
    assert not any(".deploy-releases" in e for e in events1)
    assert not any(e.startswith("node_") for e in events1)
    assert not any("restart dalton-langalpha-backend-1" in e for e in events1)
    assert not any("com.dalton.channel-gateway" in e for e in events1)
    assert (
        subprocess.check_output(["git", "-C", str(ctx1["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx1["v1"])
    )

    # Case 2: git rev-parse returns 128 on TARGET:integrations/dalton-channel-gateway
    case2 = tmp_path / "rev_parse_err"
    case2.mkdir()
    ctx2 = _setup_gateway_deploy_env(
        case2,
        gateway_changed=True,
        git_rev_parse_gw_error_rc=128,
    )
    res2 = _run_deploy(ctx2, case2)
    assert res2.returncode != 0
    assert "git rev-parse failed while checking" in res2.stderr
    assert (
        subprocess.check_output(["git", "-C", str(ctx2["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx2["v1"])
    )


def _find_real_node() -> Path:
    candidates = [
        *(Path(p) / "node" for p in os.environ.get("PATH", "").split(":") if p),
        Path("/opt/homebrew/bin/node"),
        Path("/usr/local/bin/node"),
    ]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    raise AssertionError("real node executable not found on host")


def test_real_node_esm_import_checks_pass_and_catch_missing_dependency(tmp_path: Path) -> None:
    real_node = _find_real_node()
    real_bin = tmp_path / "real_bin"
    real_bin.mkdir()
    (real_bin / "node").symlink_to(real_node)
    (real_bin / "python3").symlink_to(_REPO_ROOT / ".venv/bin/python")

    repo = tmp_path / "repo"
    gw_dir = repo / "integrations/dalton-channel-gateway"
    rel_dir = gw_dir / ".deploy-releases/rel-real-1"
    pkg_dir = rel_dir / "node_modules/@larksuiteoapi/node-sdk"
    dist_dir = rel_dir / "dist"
    pkg_dir.mkdir(parents=True)
    dist_dir.mkdir(parents=True)

    (pkg_dir / "package.json").write_text(
        '{"name":"@larksuiteoapi/node-sdk","type":"module","main":"index.mjs"}\n'
    )
    (pkg_dir / "index.mjs").write_text('export const sdkMarker = "lark-sdk-real-ok";\n')

    # pnpm .pnpm/ symlink layout positive case:
    # rel_dir/node_modules/fake-pnpm-pkg -> .pnpm/fake-pnpm-pkg@1.0.0/node_modules/fake-pnpm-pkg
    # which imports transitive sibling .pnpm/fake-pnpm-pkg@1.0.0/node_modules/fake-dep (NOT hoisted at top level!)
    pnpm_store_nm = rel_dir / "node_modules/.pnpm/fake-pnpm-pkg@1.0.0/node_modules"
    pnpm_pkg_real = pnpm_store_nm / "fake-pnpm-pkg"
    pnpm_dep_real = pnpm_store_nm / "fake-dep"
    pnpm_pkg_real.mkdir(parents=True)
    pnpm_dep_real.mkdir(parents=True)
    (pnpm_dep_real / "package.json").write_text(
        '{"name":"fake-dep","type":"module","main":"index.mjs"}\n'
    )
    (pnpm_dep_real / "index.mjs").write_text('export const depMarker = "pnpm-transitive";\n')
    (pnpm_pkg_real / "package.json").write_text(
        '{"name":"fake-pnpm-pkg","type":"module","main":"index.mjs"}\n'
    )
    (pnpm_pkg_real / "index.mjs").write_text(
        'import { depMarker } from "fake-dep";\n'
        'export const pnpmMarker = `${depMarker}-ok`;\n'
    )
    (rel_dir / "node_modules/fake-pnpm-pkg").symlink_to(
        ".pnpm/fake-pnpm-pkg@1.0.0/node_modules/fake-pnpm-pkg"
    )

    (dist_dir / "index.mjs").write_text(
        'import { sdkMarker } from "@larksuiteoapi/node-sdk";\n'
        'import { pnpmMarker } from "fake-pnpm-pkg";\n'
        'if (sdkMarker !== "lark-sdk-real-ok" || pnpmMarker !== "pnpm-transitive-ok") {\n'
        '  throw new Error("unexpected markers");\n'
        "}\n"
    )

    (gw_dir / ".deploy-current").symlink_to(".deploy-releases/rel-real-1")
    (gw_dir / "dist").symlink_to(".deploy-current/dist")
    (gw_dir / "node_modules").symlink_to(".deploy-current/node_modules")

    env = {
        "PATH": f"{real_bin}:/bin:/usr/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "DALTON_REPO_ROOT": str(repo),
    }

    ok_rel = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; '
                f'verify_gateway_release_import "{rel_dir}"'
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_rel.returncode == 0, ok_rel.stderr
    assert list(tmp_path.glob("dalton-gw-import.*")) == []

    ok_prod = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; '
                "verify_gateway_production_import"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_prod.returncode == 0, ok_prod.stderr

    # Negative isolation test: package exists ONLY in gw_dir/node_modules (parent directory of .deploy-releases/*)
    # and does NOT exist in rel_dir/node_modules -> isolated verify_gateway_release_import MUST fail and leave no temp dir!
    (gw_dir / "node_modules").unlink()
    parent_only_pkg = gw_dir / "node_modules/parent-only-pkg"
    parent_only_pkg.mkdir(parents=True)
    (parent_only_pkg / "package.json").write_text(
        '{"name":"parent-only-pkg","type":"module","main":"index.mjs"}\n'
    )
    (parent_only_pkg / "index.mjs").write_text('export const parentOnly = "leaked-from-parent";\n')
    (dist_dir / "index.mjs").write_text(
        'import { parentOnly } from "parent-only-pkg";\n'
        'if (!parentOnly) { throw new Error("missing"); }\n'
    )

    fail_parent_leak = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; '
                f'verify_gateway_release_import "{rel_dir}"'
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fail_parent_leak.returncode != 0
    assert "ERR_MODULE_NOT_FOUND" in fail_parent_leak.stderr
    assert list(tmp_path.glob("dalton-gw-import.*")) == []

    # Restore top-level node_modules symlink and bundle importing @larksuiteoapi/node-sdk,
    # then remove the dependency entry point so real Node ESM throws ERR_MODULE_NOT_FOUND
    subprocess.run(["rm", "-rf", str(gw_dir / "node_modules")], check=True)
    (gw_dir / "node_modules").symlink_to(".deploy-current/node_modules")
    (dist_dir / "index.mjs").write_text(
        'import { sdkMarker } from "@larksuiteoapi/node-sdk";\n'
        'if (sdkMarker !== "lark-sdk-real-ok") { throw new Error("unexpected sdkMarker"); }\n'
    )
    (pkg_dir / "index.mjs").unlink()

    fail_rel = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; '
                f'verify_gateway_release_import "{rel_dir}"'
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fail_rel.returncode != 0
    assert "ERR_MODULE_NOT_FOUND" in fail_rel.stderr
    assert list(tmp_path.glob("dalton-gw-import.*")) == []

    fail_prod = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; '
                "verify_gateway_production_import"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fail_prod.returncode != 0
    assert "ERR_MODULE_NOT_FOUND" in fail_prod.stderr


def test_sigterm_right_after_deploy_current_replace_preserves_active_release_and_symlinks(
    tmp_path: Path,
) -> None:
    ctx = _setup_gateway_deploy_env(tmp_path, gateway_changed=True)
    gw_dir = Path(ctx["gw_dir"])

    # Simulate SIGTERM arriving immediately after os.replace switches .deploy-current to rel-*,
    # while ACTIVE_GATEWAY_STAGING_DIR and STAGED_GATEWAY_RELEASE_ID still hold the newly active release!
    extra_setup = f"""
orig_replace=$(declare -f _atomic_replace_symlink)
eval "real_${{orig_replace}}"
_atomic_replace_symlink() {{
  real__atomic_replace_symlink "$@"
  local rc=$?
  if [ "$rc" -eq 0 ] && [[ "${{2:-}}" == *"/.deploy-current" ]] && [[ "${{1:-}}" == .deploy-releases/rel-* ]]; then
    ACTIVE_GATEWAY_STAGING_DIR="{gw_dir}/$1"
    STAGED_GATEWAY_RELEASE_ID="${{1#.deploy-releases/}}"
    kill -TERM $$
  fi
  return "$rc"
}}
"""
    res = _run_deploy(ctx, tmp_path, extra_setup=extra_setup)
    assert res.returncode == 143
    assert "preserving active gateway release .deploy-releases/rel-" in res.stderr

    assert (gw_dir / ".deploy-current").is_symlink()
    active_rel = os.readlink(gw_dir / ".deploy-current")
    assert active_rel.startswith(".deploy-releases/rel-")
    assert (gw_dir / active_rel).is_dir()
    assert (gw_dir / "dist").is_symlink()
    assert (gw_dir / "node_modules").is_symlink()
    assert (gw_dir / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v2-sdk\n"

    # Also verify cleanup_gateway_staging refuses to delete an active release pointed to by .deploy-current
    cleanup_res = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{ctx["repo"]}"; '
                f'source "{_DEPLOY_SCRIPT}"; '
                f'ACTIVE_GATEWAY_STAGING_DIR="{gw_dir}/{active_rel}"; '
                f'STAGED_GATEWAY_RELEASE_ID="{active_rel.removeprefix(".deploy-releases/")}"; '
                "cleanup_gateway_staging"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cleanup_res.returncode == 0
    assert (gw_dir / active_rel / "dist/index.mjs").read_text() == "v2-bundle\n"


def test_sigint_cleans_up_unswitched_staging_release_with_exit_130(tmp_path: Path) -> None:
    ctx = _setup_gateway_deploy_env(tmp_path, gateway_changed=True)
    gw_dir = Path(ctx["gw_dir"])
    ready_for_sigint = tmp_path / "ready_for_sigint"

    wait_idle_calls = tmp_path / "sigint_wait_idle.calls"
    wait_idle_calls.write_text("0")
    Path(ctx["wait_idle"]).write_text(
        f"""#!/bin/bash
n=$(( $(cat "{wait_idle_calls}") + 1 ))
echo "$n" > "{wait_idle_calls}"
if [ "$n" -gt 1 ]; then
  touch "{ready_for_sigint}"
  while :; do sleep 0.05; done
fi
exit 0
"""
    )
    Path(ctx["wait_idle"]).chmod(0o755)
    docker_bin = Path(ctx["fake_bin"]) / "docker"
    docker_bin.write_text(
        docker_bin.read_text().replace(
            'elif echo "$*" | grep -q "conversation_threads"; then\n      echo "0"',
            'elif echo "$*" | grep -q "conversation_threads"; then\n      echo "1"',
        )
    )

    script = f"""
set -uo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
WAIT_IDLE_SCRIPT="{ctx['wait_idle']}"
run_deploy_main --reuse-backup-minutes 60
"""
    proc = subprocess.Popen(
        ["bash", "-c", script],
        env={
            "PATH": f"{ctx['fake_bin']}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(ctx["repo"]),
            "DALTON_PG_BACKUP_LOG": str(ctx["backup_log"]),
            "DALTON_ALEMBIC_QUERY_RETRY_DELAY_S": "0",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.time() + 15
    while time.time() < deadline and not ready_for_sigint.exists():
        time.sleep(0.05)
    assert ready_for_sigint.exists(), "deploy script did not reach post-build wait_idle"
    assert list((gw_dir / ".deploy-releases").glob("rel-*"))

    os.killpg(proc.pid, signal.SIGINT)
    _stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 130
    assert "deploy interrupted by SIGINT; cleaned up unswitched gateway staging release" in stderr
    assert not list((gw_dir / ".deploy-releases").glob("rel-*"))
    assert (gw_dir / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"


@pytest.mark.skipif(
    not (sys.platform == "darwin" or sys.platform.startswith("linux")),
    reason="requires macOS RENAME_SWAP (renamex_np) or Linux RENAME_EXCHANGE (renameat2)",
)
def test_first_time_migration_rename_swap_interrupt_or_failure_has_zero_missing_window(
    tmp_path: Path,
) -> None:
    # Case 1: SIGTERM arrives right after dist is atomically swapped via RENAME_SWAP and before node_modules is swapped
    case1 = tmp_path / "swap_sigterm"
    case1.mkdir()
    ctx1 = _setup_gateway_deploy_env(case1, gateway_changed=True)
    gw_dir1 = Path(ctx1["gw_dir"])
    extra_setup1 = """
orig_swap=$(declare -f _atomic_swap_paths)
eval "real_${orig_swap}"
_atomic_swap_paths() {
  real__atomic_swap_paths "$@"
  local rc=$?
  if [ "$rc" -eq 0 ] && [[ "${2:-}" == *"/dist" ]]; then
    kill -TERM $$
  fi
  return "$rc"
}
"""
    res1 = _run_deploy(ctx1, case1, extra_setup=extra_setup1)
    assert res1.returncode == 143
    # dist is already a valid symlink to .deploy-current/dist -> prev-*/dist, and node_modules is the original v1 real dir!
    assert (gw_dir1 / "dist").is_symlink()
    assert (gw_dir1 / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir1 / "node_modules").is_dir()
    assert (gw_dir1 / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert not list((gw_dir1 / ".deploy-releases").glob("rel-*"))

    # Case 2: dist RENAME_SWAP succeeds, node_modules RENAME_SWAP fails, and dist rollback RENAME_SWAP succeeds
    case2 = tmp_path / "swap_rollback_ok"
    case2.mkdir()
    ctx2 = _setup_gateway_deploy_env(case2, gateway_changed=True)
    gw_dir2 = Path(ctx2["gw_dir"])
    extra_setup2 = """
orig_swap=$(declare -f _atomic_swap_paths)
eval "real_${orig_swap}"
_atomic_swap_paths() {
  if [[ "${2:-}" == *"/node_modules" ]]; then
    return 1
  fi
  real__atomic_swap_paths "$@"
}
"""
    res2 = _run_deploy(ctx2, case2, extra_setup=extra_setup2)
    assert res2.returncode != 0
    assert not (gw_dir2 / "dist").is_symlink()
    assert not (gw_dir2 / "node_modules").is_symlink()
    assert (gw_dir2 / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir2 / "node_modules/@larksuiteoapi/node-sdk/index.js").read_text() == "v1-sdk\n"
    assert not list(gw_dir2.glob(".deploy-legacy-backup-*"))
    assert "文件换回: 跳过（切换未完成，生产目录保持上一版完整状态）" in res2.stdout


def test_pre_ff_gateway_pending_persists_across_migrating_abort_and_first_time_force_gateway_failure(
    tmp_path: Path,
) -> None:
    # Case 1: First-time --gateway when PRE_FF_GATEWAY_TREE == TARGET_GATEWAY_TREE and gateway build fails:
    # must NOT pre-write TARGET hash to .deploy-deployed-tree, must leave .deploy-gateway-pending,
    # and subsequent normal deploy (without --gateway) must build & deploy gateway and clear .deploy-gateway-pending!
    case1 = tmp_path / "first_force_gw_same_tree"
    case1.mkdir()
    ctx1 = _setup_gateway_deploy_env(
        case1,
        gateway_changed=False,
        gateway_pnpm_rc=1,
    )
    gw_dir1 = Path(ctx1["gw_dir"])
    res1_fail = _run_deploy(ctx1, case1, args="--gateway --reuse-backup-minutes 60")
    assert res1_fail.returncode != 0
    assert not (gw_dir1 / ".deploy-deployed-tree").exists()
    assert (gw_dir1 / ".deploy-gateway-pending").exists()

    # Fix pnpm and run normal deploy WITHOUT --gateway on the exact same tree
    pnpm1 = Path(ctx1["fake_bin"]) / "pnpm"
    pnpm1.write_text(pnpm1.read_text().replace("if [ 1 -ne 0 ]", "if [ 0 -ne 0 ]"))
    res1_ok = _run_deploy(ctx1, case1, args="--reuse-backup-minutes 60")
    assert res1_ok.returncode == 0, res1_ok.stdout + res1_ok.stderr
    assert "gateway health ok" in res1_ok.stdout
    assert (gw_dir1 / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert (gw_dir1 / ".deploy-deployed-tree").read_text().strip() == str(ctx1["v2_gw_tree"])
    assert not (gw_dir1 / ".deploy-gateway-pending").exists()

    # Verify the post_ok=1 && GATEWAY=1 && GATEWAY_SWITCHED=1 && !HAD_PREEXISTING_GATEWAY_PENDING branch:
    # starting from NO .deploy-gateway-pending, when --gateway triggers a switch and passes gateway health,
    # if wait_crawler_health fails ONLY during deploy (1st call) and passes on post-revert check (2nd call),
    # revert_deploy reaches post_ok=1 yet MUST preserve .deploy-gateway-pending!
    crawler_marker = case1 / "crawler_fail_once"
    res1_switched_revert = _run_deploy(
        ctx1,
        case1,
        args="--gateway --reuse-backup-minutes 60",
        extra_setup=(
            f'wait_crawler_health() {{ '
            f'if [ ! -f "{crawler_marker}" ]; then touch "{crawler_marker}"; echo "crawler health: unreachable"; return 1; fi; '
            f'echo "crawler health: ok"; return 0; }}'
        ),
    )
    assert res1_switched_revert.returncode != 0
    assert "POST-REVERT CHECK: gateway health OK" in res1_switched_revert.stdout
    assert "POST-REVERT HEALTH REPORT: all revert steps and post-revert checks passed" in res1_switched_revert.stdout
    assert (gw_dir1 / ".deploy-gateway-pending").exists()
    (gw_dir1 / ".deploy-gateway-pending").unlink()

    # Also verify that even when .deploy-deployed-tree already matches PRE_FF_GATEWAY_TREE,
    # a subsequent failed --gateway deploy keeps .deploy-gateway-pending after revert_deploy!
    pnpm1.write_text(pnpm1.read_text().replace("if [ 0 -ne 0 ]", "if [ 1 -ne 0 ]"))
    res1_force_fail_again = _run_deploy(ctx1, case1, args="--gateway --reuse-backup-minutes 60")
    assert res1_force_fail_again.returncode != 0
    assert (gw_dir1 / ".deploy-gateway-pending").exists()

    # Case 2: Migrating deploy writes .deploy-gateway-pending BEFORE git merge --ff-only;
    # even if .deploy-deployed-tree is missing after a post-ff abort, .deploy-gateway-pending triggers gateway build on retry!
    case2 = tmp_path / "migrating_pending_before_ff"
    case2.mkdir()
    ctx2 = _setup_gateway_deploy_env(
        case2,
        gateway_changed=True,
        with_migration_change=True,
        wait_idle_fail_after_first=True,
    )
    gw_dir2 = Path(ctx2["gw_dir"])
    res2_abort = _run_deploy(ctx2, case2, args="")
    assert res2_abort.returncode != 0
    assert (gw_dir2 / ".deploy-gateway-pending").exists()
    # Even if .deploy-deployed-tree were missing, .deploy-gateway-pending guarantees retry builds gateway
    (gw_dir2 / ".deploy-deployed-tree").unlink(missing_ok=True)

    Path(ctx2["wait_idle"]).write_text("#!/bin/bash\nexit 0\n")
    docker2 = Path(ctx2["fake_bin"]) / "docker"
    docker2.write_text(docker2.read_text().replace('echo "1"', 'echo "0"'))

    res2_retry = _run_deploy(ctx2, case2, args="")
    assert res2_retry.returncode == 0, res2_retry.stdout + res2_retry.stderr
    assert "gateway health ok" in res2_retry.stdout
    assert (gw_dir2 / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert (gw_dir2 / ".deploy-deployed-tree").read_text().strip() == str(ctx2["v2_gw_tree"])
    assert not (gw_dir2 / ".deploy-gateway-pending").exists()


def test_restore_gateway_tree_write_failure_returns_nonzero_and_preserves_pending_and_release(
    tmp_path: Path,
) -> None:
    ctx = _setup_gateway_deploy_env(
        tmp_path,
        gateway_changed=True,
        with_migration_change=True,
        crawler_health_fail=True,
    )
    gw_dir = Path(ctx["gw_dir"])
    write_calls = tmp_path / "tree_write_calls"
    write_calls.write_text("0")

    # Call 1 (pre-ff initial write of v1) and Call 2 (post-gateway-health write of v2) succeed;
    # Call 3 (inside restore_gateway_from_prev during revert_deploy) fails!
    extra_setup = f"""
wait_crawler_health() {{ echo "crawler health: unreachable"; return 1; }}
orig_write_tree=$(declare -f _write_gateway_deployed_tree)
eval "real_${{orig_write_tree}}"
_write_gateway_deployed_tree() {{
  local n=$(( $(cat "{write_calls}") + 1 ))
  echo "$n" > "{write_calls}"
  if [ "$n" -ge 3 ]; then
    return 1
  fi
  real__write_gateway_deployed_tree "$@"
}}
"""
    res = _run_deploy(ctx, tmp_path, args="", extra_setup=extra_setup)
    assert res.returncode != 0
    assert "ERROR: failed to write restored gateway tree hash" in res.stderr
    assert "gateway_switched=1, gateway_revert_state=restore_failed" in res.stdout
    assert "gateway_revert_state=reverted" not in res.stdout
    assert (gw_dir / ".deploy-gateway-pending").exists()
    # Outgoing rel-* directory must be preserved when restore_gateway_from_prev fails!
    assert list((gw_dir / ".deploy-releases").glob("rel-*"))
    assert list((gw_dir / ".deploy-releases").glob("prev-*"))


def test_restore_gateway_rejects_unconfirmed_or_corrupt_candidates(tmp_path: Path) -> None:
    ctx = _setup_gateway_deploy_env(tmp_path, gateway_changed=True)
    repo = Path(ctx["repo"])
    gw_dir = Path(ctx["gw_dir"])

    rel_cur = gw_dir / ".deploy-releases/rel-current"
    rel_unconfirmed = gw_dir / ".deploy-releases/rel-unconfirmed-leftover"
    for d, ver in ((rel_cur, "v2"), (rel_unconfirmed, "v1")):
        (d / "dist").mkdir(parents=True)
        (d / "dist/index.mjs").write_text(f"{ver}-bundle\n")
        (d / "node_modules/@larksuiteoapi/node-sdk").mkdir(parents=True)
        (d / "node_modules/@larksuiteoapi/node-sdk/index.js").write_text(f"{ver}-sdk\n")

    subprocess.run(["rm", "-rf", str(gw_dir / "dist"), str(gw_dir / "node_modules")], check=True)
    (gw_dir / ".deploy-current").symlink_to(".deploy-releases/rel-current")
    (gw_dir / "dist").symlink_to(".deploy-current/dist")
    (gw_dir / "node_modules").symlink_to(".deploy-current/node_modules")

    # Case 1: PREV_GATEWAY_RELEASE_REL is empty -> must NOT blindly pick rel-unconfirmed-leftover from .deploy-releases/*
    res1 = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo}"; '
                f'source "{_DEPLOY_SCRIPT}"; PREV_GATEWAY_RELEASE_REL=""; '
                "restore_gateway_from_prev"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode != 0
    assert os.readlink(gw_dir / ".deploy-current") == ".deploy-releases/rel-current"
    assert rel_cur.is_dir()

    # Case 2: PREV_GATEWAY_RELEASE_REL points to a candidate whose bundle/sdk version pair fails verify_gateway_release_import
    (rel_unconfirmed / "dist/index.mjs").write_text("v1-bundle\n")
    (rel_unconfirmed / "node_modules/@larksuiteoapi/node-sdk/index.js").write_text("v999-mismatched-sdk\n")
    res2 = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo}"; '
                f'source "{_DEPLOY_SCRIPT}"; '
                'PREV_GATEWAY_RELEASE_REL=".deploy-releases/rel-unconfirmed-leftover"; '
                "restore_gateway_from_prev"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode != 0
    assert "failed pre-restore import check; refusing rollback switch" in res2.stderr
    assert os.readlink(gw_dir / ".deploy-current") == ".deploy-releases/rel-current"
    assert rel_cur.is_dir()


def test_manual_switch_and_rebuild_helpers_and_no_in_place_build_instructions(
    tmp_path: Path,
) -> None:
    script_text = _DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "pnpm install --frozen-lockfile && node build.mjs" not in script_text

    ctx = _setup_gateway_deploy_env(tmp_path, gateway_changed=True)
    res_init = _run_deploy(ctx, tmp_path)
    assert res_init.returncode == 0, res_init.stdout + res_init.stderr

    repo = Path(ctx["repo"])
    gw_dir = Path(ctx["gw_dir"])
    prev_dirs = sorted(p.name for p in (gw_dir / ".deploy-releases").glob("prev-*"))
    rel_dirs = sorted(p.name for p in (gw_dir / ".deploy-releases").glob("rel-*"))
    assert len(prev_dirs) == 1 and len(rel_dirs) == 1

    # 1. manual_switch_gateway_release repairs a deleted top-level dist symlink, switches to prev-*,
    # and preserves .deploy-gateway-pending (does NOT clear it).
    (gw_dir / "dist").unlink()
    (gw_dir / ".deploy-gateway-pending").write_text("pending\n")
    assert not (gw_dir / "dist").exists()

    res_switch = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo}"; '
                f'source "{_DEPLOY_SCRIPT}"; '
                f'manual_switch_gateway_release ".deploy-releases/{prev_dirs[0]}"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_switch.returncode == 0, res_switch.stdout + res_switch.stderr
    assert (gw_dir / "dist").is_symlink()
    assert os.readlink(gw_dir / "dist") == ".deploy-current/dist"
    assert os.readlink(gw_dir / ".deploy-current") == f".deploy-releases/{prev_dirs[0]}"
    assert (gw_dir / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == str(ctx["v1_gw_tree"])
    assert (gw_dir / ".deploy-gateway-pending").exists()

    # 1b. When manual_switch_gateway_release fails (e.g. production import check fails),
    # .deploy-current and top-level symlinks roll back cleanly (no half-switch) and .deploy-gateway-pending is preserved!
    (gw_dir / "dist").unlink()
    res_switch_fail = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo}"; '
                f'source "{_DEPLOY_SCRIPT}"; '
                "verify_gateway_production_import() { return 1; }; "
                f'manual_switch_gateway_release ".deploy-releases/{rel_dirs[0]}"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_switch_fail.returncode != 0
    assert os.readlink(gw_dir / ".deploy-current") == f".deploy-releases/{prev_dirs[0]}"
    assert not (gw_dir / "dist").exists()
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == str(ctx["v1_gw_tree"])
    assert (gw_dir / ".deploy-gateway-pending").exists()
    # Restore dist symlink to .deploy-current/dist before testing manual_rebuild_gateway_release failure
    (gw_dir / "dist").symlink_to(".deploy-current/dist")

    # 2. manual_rebuild_gateway_release when node build.mjs fails mid-way MUST NOT overwrite the active release!
    node_bin = Path(ctx["fake_bin"]) / "node"
    orig_node_script = node_bin.read_text()
    node_bin.write_text(
        orig_node_script.replace(
            'echo "${ver_tag}-bundle" > dist/index.mjs\n  exit 0',
            'echo "corrupt-in-place" > dist/index.mjs\n  exit 1',
        )
    )
    res_rebuild_fail = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo}"; '
                f'source "{_DEPLOY_SCRIPT}"; '
                "manual_rebuild_gateway_release"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_rebuild_fail.returncode != 0
    assert (gw_dir / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert os.readlink(gw_dir / ".deploy-current") == f".deploy-releases/{prev_dirs[0]}"

    # 3. Restore node build success, remove top-level dist symlink, copy deploy_main.sh to /tmp-style external path,
    # and run manual_rebuild_gateway_release with explicit DALTON_REPO_ROOT as printed by Plan B Step 8:
    # builds in a fresh release directory, repairs top-level dist symlink, switches .deploy-current, updates tree hash, and clears pending!
    node_bin.write_text(orig_node_script)
    (gw_dir / "dist").unlink()
    (gw_dir / ".deploy-gateway-pending").write_text("pending\n")
    tmp_script_copy = tmp_path / "dalton_deploy_main.sh"
    tmp_script_copy.write_text(_DEPLOY_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    res_rebuild_ok = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_REPO_ROOT="{repo}" DALTON_DEPLOY_LIB_ONLY=1 '
                f'bash -c \'source "{tmp_script_copy}" && manual_rebuild_gateway_release\''
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_rebuild_ok.returncode == 0, res_rebuild_ok.stdout + res_rebuild_ok.stderr
    assert (gw_dir / "dist").is_symlink()
    assert (gw_dir / "dist/index.mjs").read_text() == "v2-bundle\n"
    assert (gw_dir / ".deploy-deployed-tree").read_text().strip() == str(ctx["v2_gw_tree"])
    assert not (gw_dir / ".deploy-gateway-pending").exists()

    # 4. When orig_had_prev_current=0, dist is a real directory, node_modules is missing, and production import fails:
    # manual_switch_gateway_release rolls back cleanly to real dist + missing node_modules + no .deploy-current (zero half-switch)!
    (gw_dir / ".deploy-current").unlink()
    (gw_dir / "dist").unlink()
    (gw_dir / "node_modules").unlink()
    (gw_dir / "dist").mkdir()
    (gw_dir / "dist/index.mjs").write_text("real-unpaired-dist\n")

    res_unpaired_fail = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo}"; '
                f'source "{_DEPLOY_SCRIPT}"; '
                "verify_gateway_production_import() { return 1; }; "
                f'manual_switch_gateway_release ".deploy-releases/{prev_dirs[0]}"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_unpaired_fail.returncode != 0
    assert not (gw_dir / ".deploy-current").exists() and not (gw_dir / ".deploy-current").is_symlink()
    assert (gw_dir / "dist").is_dir() and not (gw_dir / "dist").is_symlink()
    assert (gw_dir / "dist/index.mjs").read_text() == "real-unpaired-dist\n"
    assert not (gw_dir / "node_modules").exists() and not (gw_dir / "node_modules").is_symlink()
    assert not list(gw_dir.glob(".deploy-legacy-backup-*"))

    # 5. When _rollback_manual_switch fails to swap dist back from backup_dir, backup_dir MUST be preserved!
    swap_count_file = tmp_path / "manual_swap_count"
    swap_count_file.write_text("0")
    res_swapback_fail = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo}"; '
                f'source "{_DEPLOY_SCRIPT}"; '
                'orig_swap=$(declare -f _atomic_swap_paths); eval "real_${orig_swap}"; '
                f'_atomic_swap_paths() {{ local n=$(( $(cat "{swap_count_file}") + 1 )); echo "$n" > "{swap_count_file}"; '
                'if [ "$n" -eq 1 ]; then real__atomic_swap_paths "$@"; return $?; fi; return 1; }; '
                "verify_gateway_production_import() { return 1; }; "
                f'manual_switch_gateway_release ".deploy-releases/{prev_dirs[0]}"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_swapback_fail.returncode != 0
    assert "ERROR: manual_switch_gateway_release rollback: failed to swap back" in res_swapback_fail.stderr
    preserved_backups = list(gw_dir.glob(".deploy-legacy-backup-*"))
    assert len(preserved_backups) == 1
    assert (preserved_backups[0] / "dist/index.mjs").read_text() == "real-unpaired-dist\n"

    # 6. When orig_had_prev_current=0 with BOTH real dist/ and real node_modules/, if production import fails
    # and _rollback_manual_switch fails to swap node_modules back while dist swap-back succeeds,
    # .deploy-current MUST be restored to prev-manual-* (not left pointing at the failed target release)!
    case6 = tmp_path / "manual_rollback_restores_prev_manual"
    case6.mkdir()
    ctx6 = _setup_gateway_deploy_env(case6, gateway_changed=True)
    gw6 = Path(ctx6["gw_dir"])
    repo6 = Path(ctx6["repo"])
    rel_target = gw6 / ".deploy-releases/rel-target"
    (rel_target / "dist").mkdir(parents=True)
    (rel_target / "dist/index.mjs").write_text("export const v = 'target';\n")
    (rel_target / "node_modules/pkg").mkdir(parents=True)
    (rel_target / "node_modules/pkg/index.js").write_text("module.exports = 'target';\n")
    (gw6 / ".deploy-current").unlink(missing_ok=True)
    for dname in ("dist", "node_modules"):
        p = gw6 / dname
        if p.is_symlink():
            p.unlink()
        elif p.exists():
            shutil.rmtree(p)
    (gw6 / "dist").mkdir()
    (gw6 / "dist/index.mjs").write_text("export const v = 'legacy';\n")
    (gw6 / "node_modules/pkg").mkdir(parents=True)
    (gw6 / "node_modules/pkg/index.js").write_text("module.exports = 'legacy';\n")
    swap6 = tmp_path / "manual_swap_count_6"
    swap6.write_text("0")
    res6 = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; export PATH="{ctx6["fake_bin"]}:/bin:/usr/bin"; '
                f'DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo6}"; '
                f'source "{_DEPLOY_SCRIPT}"; '
                'orig_swap=$(declare -f _atomic_swap_paths); eval "real_${orig_swap}"; '
                f'_atomic_swap_paths() {{ local n=$(( $(cat "{swap6}") + 1 )); echo "$n" > "{swap6}"; '
                'if [ "$n" -le 3 ]; then real__atomic_swap_paths "$@"; return $?; fi; return 1; }; '
                'verify_gateway_release_import() { return 0; }; '
                'verify_gateway_production_import() { return 1; }; '
                'manual_switch_gateway_release ".deploy-releases/rel-target"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res6.returncode != 0
    assert (gw6 / ".deploy-current").is_symlink()
    cur6 = os.readlink(gw6 / ".deploy-current")
    assert cur6.startswith(".deploy-releases/prev-manual-"), f"Expected prev-manual-*, got {cur6}"


def test_cross_deploy_honest_tree_reporting_and_revert_deploy_gateway_health_recheck(
    tmp_path: Path,
) -> None:
    # Scenario A: Deploy 1 fast-forwards Git v1 -> v2, switches gateway to v2, then gateway health check fails ->
    # rollback_gateway_only leaves HEAD=v2 and gateway=v1 (v1_gw_tree).
    # Deploy 2 runs on v2 (so PRE_FF == v2, PRE_FF_GATEWAY_TREE == v2_gw_tree) with a pending migration,
    # switches gateway to v2 and passes gateway health, then fails at wait_crawler_health ->
    # revert_deploy restores gateway to v1 (v1_gw_tree).
    # Plan B Step 8 and disk report MUST report actual restored tree hash v1_gw_tree (NOT v2_gw_tree / PRE_FF)!
    case_a = tmp_path / "cross_deploy_honest_tree"
    case_a.mkdir()
    ctx_a = _setup_gateway_deploy_env(
        case_a,
        gateway_changed=True,
        with_migration_change=True,
        gateway_health_mode="ready_false_until_restored",
    )
    gw_dir_a = Path(ctx_a["gw_dir"])
    res_a1 = _run_deploy(ctx_a, case_a, args="", gateway_timeout="1", gateway_poll="1")
    assert res_a1.returncode != 0
    assert (gw_dir_a / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir_a / ".deploy-deployed-tree").read_text().strip() == str(ctx_a["v1_gw_tree"])
    assert (
        subprocess.check_output(["git", "-C", str(ctx_a["repo"]), "rev-parse", "HEAD"], text=True).strip()
        == str(ctx_a["v2"])
    )

    # Now in Deploy 2, gateway health succeeds, but crawler health fails so revert_deploy runs with PRE_FF == v2
    curl_a = Path(ctx_a["fake_bin"]) / "curl"
    curl_a.write_text(
        curl_a.read_text().replace(
            'echo \'{"ok":true,"ready":false}\'',
            'echo \'{"ok":true,"ready":true}\'',
        )
    )
    res_a2 = _run_deploy(
        ctx_a,
        case_a,
        args="",
        extra_setup='wait_crawler_health() { echo "crawler health: unreachable"; return 1; }',
    )
    assert res_a2.returncode != 0
    assert (gw_dir_a / "dist/index.mjs").read_text() == "v1-bundle\n"
    assert (gw_dir_a / ".deploy-deployed-tree").read_text().strip() == str(ctx_a["v1_gw_tree"])
    out_a2 = res_a2.stdout
    assert f"PRE_FF={ctx_a['v2']}" in out_a2
    assert f"实际恢复的网关树哈希={ctx_a['v1_gw_tree']}" in out_a2
    assert "若 RESET_SHA 等于 PRE_FF 无需再次删改或换回" not in out_a2
    assert 'git rev-parse "${RESET_SHA}:integrations/dalton-channel-gateway"' in out_a2

    # Scenario B: Non-migrating revert_deploy where gateway kickstart succeeds on revert,
    # but wait_gateway_health fails during post-revert health checks -> must report gateway_health failure and return non-zero!
    case_b = tmp_path / "revert_gw_health_fail"
    case_b.mkdir()
    ctx_b = _setup_gateway_deploy_env(
        case_b,
        gateway_changed=True,
        with_migration_change=False,
    )
    gw_health_calls_b = case_b / "gw_health_calls"
    gw_health_calls_b.write_text("0")
    extra_setup_b = f"""
wait_crawler_health() {{ echo "crawler health: unreachable"; return 1; }}
orig_wait_gw=$(declare -f wait_gateway_health)
eval "real_${{orig_wait_gw}}"
wait_gateway_health() {{
  local n=$(( $(cat "{gw_health_calls_b}") + 1 ))
  echo "$n" > "{gw_health_calls_b}"
  if [ "$n" -eq 1 ]; then
    real_wait_gateway_health "$@"
    return $?
  fi
  echo "gateway health: failed on post-revert check"
  return 1
}}
"""
    res_b = _run_deploy(ctx_b, case_b, extra_setup=extra_setup_b)
    assert res_b.returncode != 0
    assert "POST-REVERT CHECK: gateway health FAILED" in res_b.stdout
    assert "POST-REVERT REPORT: FAILED" in res_b.stdout
    assert "gateway_health" in res_b.stdout
    assert "all revert steps and post-revert checks passed" not in res_b.stdout


@pytest.mark.parametrize("with_workspace_file", [True, False])
def test_gateway_staging_copies_pnpm_workspace_file_when_present(
    tmp_path: Path, with_workspace_file: bool
) -> None:
    """pnpm 10+ needs the build-script approvals in pnpm-workspace.yaml; older revisions lack the file."""
    repo = tmp_path / "repo"
    gw = repo / "integrations/dalton-channel-gateway"
    (gw / "src").mkdir(parents=True)
    (gw / "package.json").write_text('{"name":"@dalton/channel-gateway","type":"module"}\n')
    (gw / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (gw / "build.mjs").write_text("// build\n")
    (gw / "tsconfig.json").write_text("{}\n")
    (gw / "src/main.ts").write_text("export const v = 1;\n")
    if with_workspace_file:
        (gw / "pnpm-workspace.yaml").write_text("allowBuilds:\n  esbuild: true\n")

    trace = tmp_path / "pnpm.trace"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "pnpm").write_text(
        f'#!/bin/bash\nif [ -f pnpm-workspace.yaml ]; then echo ws=yes >> "{trace}"; '
        f'else echo ws=no >> "{trace}"; fi\nmkdir -p node_modules\nexit 0\n'
    )
    (fake_bin / "node").write_text("#!/bin/bash\nmkdir -p dist\necho bundle > dist/index.mjs\nexit 0\n")
    for name in ("pnpm", "node"):
        (fake_bin / name).chmod(0o755)

    res = subprocess.run(
        [
            "bash",
            "-c",
            (
                f'set -uo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; '
                f'REPO_ROOT="{repo}"; TARGET_GATEWAY_TREE=deadbeef; '
                'build_gateway_staging; echo "rc=$?"; echo "staging=$ACTIVE_GATEWAY_STAGING_DIR"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert "rc=0" in res.stdout, res.stdout + res.stderr
    assert trace.read_text().strip() == ("ws=yes" if with_workspace_file else "ws=no")
