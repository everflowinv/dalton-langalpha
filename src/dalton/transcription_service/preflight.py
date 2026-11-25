"""Shared preflight validation and LaunchAgent path resolution for ``dalton-transcription``.

Used by ``scripts/dalton/ops/prod/switch_transcription_service.sh`` so the cutover
script shares the exact same URL / adapter / binary validation logic as
:func:`src.dalton.transcription_service.worker_registry.validate_worker_startup_config`
and resolves LaunchAgent ``DALTON_ASR_JOBS_DIR`` / ``DALTON_ASR_USAGE_FILE`` paths
using each plist's actual ``EnvironmentVariables``, custom env file, and
``WorkingDirectory``.

Keep top-level imports stdlib-only so ``--resolve-paths`` runs cleanly even under
minimal system ``python3`` without optional web/storage dependencies.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path

_PLACEHOLDERS: tuple[str, ...] = (
    "__PYTHON_BIN__",
    "__REPO__",
    "__REPO_ROOT__",
    "__HOME__",
    "__LOG_DIR__",
    "__RESEARCH_CODE__",
    "__RESEARCH_HOME__",
)


def _is_placeholder(val: str) -> bool:
    return any(tok in val for tok in _PLACEHOLDERS)


def _expand_path(raw: str, home_dir: str | None) -> Path:
    if home_dir:
        if raw == "~":
            return Path(home_dir)
        if raw.startswith("~/"):
            return Path(home_dir) / raw[2:]
    return Path(raw).expanduser()


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple ``KEY=VALUE`` (and ``export KEY=VALUE``) lines from an env file."""
    vals: dict[str, str] = {}
    if not path.is_file():
        return vals
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            vals[k] = v
    return vals


@dataclass(frozen=True)
class LaunchAgentContext:
    """Resolved runtime environment and path roots for a LaunchAgent plist."""

    side: str
    plist_exists: bool
    plist_env: dict[str, str]
    env_file: Path | None
    merged_env: dict[str, str]
    working_dir: Path
    service_repo: Path
    home_dir: str


def load_launchagent_context(
    *,
    side: str,
    plist_path: Path,
    fallback_env_file: Path | None,
    repo_root: Path,
) -> LaunchAgentContext:
    """Load effective LaunchAgent env, env file, working directory, and repo root.

    Matches both services' runtime precedence:
    ``plist EnvironmentVariables`` overrides the LaunchAgent's configured env file.
    Relative paths (including a relative env file path) resolve against the plist's
    ``WorkingDirectory``.
    """
    resolved_repo = repo_root.resolve()
    plist_exists = plist_path.is_file()
    plist_env: dict[str, str] = {}
    working_dir = resolved_repo
    service_repo = resolved_repo
    home_dir = os.environ.get("HOME") or str(Path.home())

    plist_data: dict[str, object] = {}
    if plist_exists:
        try:
            with plist_path.open("rb") as fh:
                loaded = plistlib.load(fh)
            if isinstance(loaded, dict):
                plist_data = loaded
        except Exception:
            plist_data = {}

    raw_env = plist_data.get("EnvironmentVariables")
    if isinstance(raw_env, dict):
        for k, v in raw_env.items():
            sv = str(v or "").strip()
            if sv and not _is_placeholder(sv):
                plist_env[str(k)] = sv

    if plist_env.get("HOME"):
        home_dir = plist_env["HOME"]

    raw_wd = str(plist_data.get("WorkingDirectory") or "").strip()
    if raw_wd and not _is_placeholder(raw_wd):
        wd_path = _expand_path(raw_wd, home_dir)
        working_dir = wd_path if wd_path.is_absolute() else (resolved_repo / wd_path)

    if side == "bridge":
        prog_args = plist_data.get("ProgramArguments")
        if isinstance(prog_args, list) and len(prog_args) >= 2:
            script_cand = str(prog_args[1] or "").strip()
            if script_cand and not _is_placeholder(script_cand):
                sp = _expand_path(script_cand, home_dir)
                if not sp.is_absolute():
                    sp = working_dir / sp
                if sp.name == "researchdata_bridge.py" and len(sp.parents) >= 3:
                    service_repo = sp.parents[2]

    env_file: Path | None
    if side == "bridge":
        raw_ef = plist_env.get("DALTON_BRIDGE_ENV_FILE", "").strip()
        if raw_ef:
            ef = _expand_path(raw_ef, home_dir)
            env_file = ef if ef.is_absolute() else (working_dir / ef)
        elif plist_exists and "HOME" in plist_env:
            env_file = Path(home_dir) / ".config" / "dalton" / "researchdata-bridge.env"
        else:
            env_file = fallback_env_file
    else:
        raw_ef = plist_env.get("DALTON_TRANSCRIPTION_ENV_FILE", "").strip()
        if raw_ef:
            ef = _expand_path(raw_ef, home_dir)
            env_file = ef if ef.is_absolute() else (working_dir / ef)
        elif plist_exists:
            # src/dalton/transcription_service/__main__.py loads an env file only when
            # DALTON_TRANSCRIPTION_ENV_FILE is explicitly set in process env.
            env_file = None
        else:
            env_file = fallback_env_file

    merged_env = parse_env_file(env_file) if env_file is not None else {}
    merged_env.update(plist_env)

    return LaunchAgentContext(
        side=side,
        plist_exists=plist_exists,
        plist_env=plist_env,
        env_file=env_file,
        merged_env=merged_env,
        working_dir=working_dir,
        service_repo=service_repo,
        home_dir=home_dir,
    )


def resolve_side_paths(
    *,
    side: str,
    plist_path: Path,
    fallback_env_file: Path | None,
    repo_root: Path,
    shell_jobs: str = "",
    shell_usage: str = "",
) -> tuple[str, str]:
    """Resolve canonical ``(jobs_dir, usage_file)`` for a LaunchAgent side."""
    ctx = load_launchagent_context(
        side=side,
        plist_path=plist_path,
        fallback_env_file=fallback_env_file,
        repo_root=repo_root,
    )
    jobs_raw = (ctx.merged_env.get("DALTON_ASR_JOBS_DIR") or "").strip()
    if not jobs_raw and not ctx.plist_exists and shell_jobs.strip():
        jobs_raw = shell_jobs.strip()
    if jobs_raw:
        jobs_path = _expand_path(jobs_raw, ctx.home_dir)
        if not jobs_path.is_absolute():
            jobs_path = ctx.working_dir / jobs_path
    else:
        jobs_path = ctx.service_repo / "var" / "dalton" / "asr_jobs"
    jobs_norm = os.path.realpath(str(jobs_path))

    usage_raw = (ctx.merged_env.get("DALTON_ASR_USAGE_FILE") or "").strip()
    if not usage_raw and not ctx.plist_exists and shell_usage.strip():
        usage_raw = shell_usage.strip()
    if usage_raw:
        usage_path = _expand_path(usage_raw, ctx.home_dir)
        if not usage_path.is_absolute():
            usage_path = ctx.working_dir / usage_path
    else:
        usage_path = Path(jobs_norm) / "asr_usage.jsonl"
    usage_norm = os.path.realpath(str(usage_path))

    return jobs_norm, usage_norm


def check_runtime_config(
    *,
    plist_path: Path,
    fallback_env_file: Path | None,
    repo_root: Path,
) -> int:
    """Validate transcription adapter config & ffmpeg/ffprobe using the shared validator."""
    from .worker_registry import validate_worker_startup_config

    ctx = load_launchagent_context(
        side="transcription",
        plist_path=plist_path,
        fallback_env_file=fallback_env_file,
        repo_root=repo_root,
    )
    merged_env = dict(ctx.merged_env)
    if "PATH" not in merged_env and os.environ.get("PATH"):
        merged_env["PATH"] = os.environ["PATH"]

    validation = validate_worker_startup_config(environ=merged_env)
    if not validation.ok:
        missing_csv = ", ".join(validation.missing_config)
        print(
            f"ERROR: Transcription runtime pre-check failed (adapter={validation.adapter}, "
            f"worker_mode={validation.worker_mode}): missing or invalid configuration: {missing_csv}",
            file=sys.stderr,
        )
        return 1

    print(
        f"TRANSCRIPTION_ADAPTER:  OK (adapter={validation.adapter}, "
        f"worker_mode={validation.worker_mode}, ffmpeg=OK, ffprobe=OK)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.dalton.transcription_service.preflight",
        description="Preflight validation and LaunchAgent path resolution for dalton-transcription.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-runtime",
        action="store_true",
        help="Validate transcription worker config, URLs, and ffmpeg/ffprobe.",
    )
    mode.add_argument(
        "--resolve-paths",
        action="store_true",
        help="Print canonical jobs_dir and usage_file (two lines) for a LaunchAgent side.",
    )
    parser.add_argument("--side", choices=("bridge", "transcription"), default="transcription")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--plist", required=True)
    parser.add_argument("--env-file", default="")
    parser.add_argument("--shell-jobs", default="")
    parser.add_argument("--shell-usage", default="")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    plist_path = Path(args.plist)
    fallback_env = Path(args.env_file) if args.env_file else None

    if args.check_runtime:
        return check_runtime_config(
            plist_path=plist_path,
            fallback_env_file=fallback_env,
            repo_root=repo_root,
        )

    jobs_norm, usage_norm = resolve_side_paths(
        side=args.side,
        plist_path=plist_path,
        fallback_env_file=fallback_env,
        repo_root=repo_root,
        shell_jobs=args.shell_jobs,
        shell_usage=args.shell_usage,
    )
    print(jobs_norm)
    print(usage_norm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
