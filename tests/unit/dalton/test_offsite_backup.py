"""Unit tests for Dalton offsite config & weekly data backup scripts."""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_DIR = REPO_ROOT / "scripts" / "dalton" / "ops"
sys.path.insert(0, str(OPS_DIR))

import backup_lib  # noqa: E402


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _write_test_env(
    env_path: Path,
    *,
    home_dir: Path,
    repo_dir: Path,
    key_file: Path,
    alerts_file: Path,
    log_file: Path,
    prod_checkout: Path,
    prod_var: Path,
    user_config: Path,
    launchagents: Path,
    backup_scripts: Path,
    colima: Path,
    ext_mount: Path | None = None,
    max_file_mb: int = 20,
    push_retry_delay: int = 0,
) -> None:
    ext = ext_mount or (home_dir / "Volumes" / "EveSSD")
    env_path.write_text(
        "\n".join(
            [
                f'DALTON_OFFSITE_BACKUP_LOG_FILE="{log_file}"',
                f'DALTON_BACKUP_ALERTS_FILE="{alerts_file}"',
                f'DALTON_BACKUP_KEY_FILE="{key_file}"',
                f'DALTON_CONFIG_BACKUP_REPO_DIR="{repo_dir}"',
                f'DALTON_PROD_CHECKOUT_DIR="{prod_checkout}"',
                f'DALTON_PROD_VAR_DIR="{prod_var}"',
                f'DALTON_USER_CONFIG_DIR="{user_config}"',
                f'DALTON_LAUNCHAGENTS_DIR="{launchagents}"',
                f'DALTON_BACKUP_SCRIPTS_DIR="{backup_scripts}"',
                f'DALTON_COLIMA_DIR="{colima}"',
                f'DALTON_CONFIG_MAX_FILE_MB="{max_file_mb}"',
                f'DALTON_GIT_PUSH_RETRY_DELAY_SEC="{push_retry_delay}"',
                f'DALTON_EXTERNAL_MOUNT_ROOT="{ext}"',
                f'DALTON_WEEKLY_BACKUP_ROOT="{ext}/Dalton/backups/weekly"',
                f'DALTON_EXTERNAL_TMP_ROOT="{ext}/Dalton/backups/.tmp"',
                f'DALTON_RCLONE_CACHE_DIR="{ext}/Dalton/backups/.rclone-cache"',
                f'DALTON_RCLONE_LOG_DIR="{ext}/Dalton/backups/.rclone-logs"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_secret_detection_filenames_and_contents(tmp_path: Path) -> None:
    assert backup_lib.detect_secret_reason(".env", b"APP_ENV=prod\n") is not None
    assert backup_lib.detect_secret_reason("prod/.env.production", b"PORT=8000\n") is not None
    assert backup_lib.detect_secret_reason("channel-gateway.env", b"FOO=bar\n") is not None
    assert backup_lib.detect_secret_reason("certs/server.key", b"opaque") is not None
    assert backup_lib.detect_secret_reason("auth/oauth_token.json", b'{"a":1}') is not None

    assert (
        backup_lib.detect_secret_reason(
            "config.yaml", b"openai_api_key: sk-test-123456\n"
        )
        is not None
    )
    assert (
        backup_lib.detect_secret_reason(
            "settings.sh", b'export SERVICE_TOKEN="tok_abc123"\n'
        )
        is not None
    )
    assert (
        backup_lib.detect_secret_reason(
            "creds.json", b'{"aws_access_key": "AKIA123456"}\n'
        )
        is not None
    )
    assert (
        backup_lib.detect_secret_reason(
            "com.dalton.plist",
            b"<dict><key>DB_PASSWORD</key><string>hunter2</string></dict>",
        )
        is not None
    )
    assert (
        backup_lib.detect_secret_reason(
            "colima/_lima/_config/user",
            b"-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n",
        )
        is not None
    )

    # Benign config with no secret values should remain plaintext.
    assert (
        backup_lib.detect_secret_reason(
            "agent_config.dalton.yaml",
            b"# API_KEY=example\nmodel: gpt-5\nEMPTY_TOKEN=\n",
        )
        is None
    )
    assert (
        backup_lib.detect_secret_reason(
            "notes.yaml", b'optional_secret: ""\nport: 8000\n'
        )
        is None
    )

    key_file = tmp_path / "backup.key"
    assert backup_lib.is_forbidden_key_file(key_file, key_file) is True
    assert backup_lib.is_forbidden_key_file(tmp_path / "sub" / "backup.key") is True
    assert backup_lib.is_forbidden_key_file(tmp_path / "settings.json", key_file) is False


def test_stage_excludes_size_cap_and_hash_manifest_reuse(tmp_path: Path) -> None:
    prod_checkout = tmp_path / "prod"
    prod_var = prod_checkout / "var" / "dalton"
    user_cfg = tmp_path / "dot_config_dalton"
    launchagents = tmp_path / "LaunchAgents"
    backup_scripts = tmp_path / "Backups_dalton"
    colima = tmp_path / "colima"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True)

    # Populate var/dalton with kept + excluded + oversized files.
    (prod_var / "openclaw-data" / "temp").mkdir(parents=True)
    (prod_var / "openclaw-data" / "temp" / "drop.txt").write_text("temp", encoding="utf-8")
    (prod_var / "openclaw-data" / "research-outputs").mkdir(parents=True)
    (prod_var / "openclaw-data" / "research-outputs" / "big.pdf").write_text("pdf", encoding="utf-8")
    (prod_var / "pr_baseline").mkdir(parents=True)
    (prod_var / "pr_baseline" / "x.json").write_text("{}", encoding="utf-8")
    (prod_var / "tmp").mkdir(parents=True)
    (prod_var / "tmp" / "t.txt").write_text("tmp", encoding="utf-8")
    (prod_var / ".dalton-runs").mkdir(parents=True)
    (prod_var / ".dalton-runs" / "r.json").write_text("{}", encoding="utf-8")
    (prod_var / "backups").mkdir(parents=True)
    (prod_var / "backups" / "old.tar").write_text("bak", encoding="utf-8")
    (prod_var / "state.sqlite").write_text("sqlite", encoding="utf-8")
    (prod_var / "pg.dump").write_text("dump", encoding="utf-8")
    (prod_var / "runtime_state.yaml").write_text("healthy: true\n", encoding="utf-8")
    (prod_var / "README.md").write_text("# Dalton var docs\n", encoding="utf-8")
    (prod_var / "deps.lock").write_text("lock-v1\n", encoding="utf-8")
    (prod_var / "notes.txt").write_text("plain note in var\n", encoding="utf-8")
    (prod_var / "oversized.bin").write_bytes(b"x" * 2048)

    # Populate ~/.config/dalton with backup.key (forbidden), secret env, and non-secret json.
    user_cfg.mkdir(parents=True)
    key_file = user_cfg / "backup.key"
    key_file.write_text("secret-passphrase\n", encoding="utf-8")
    (user_cfg / "channel-gateway.env").write_text(
        "INTERNAL_SERVICE_TOKEN=abc123456789\n", encoding="utf-8"
    )
    (user_cfg / "conversation-archive-state.json").write_text(
        '{"enabled": true}\n', encoding="utf-8"
    )

    # Populate launchagents, backup scripts, colima, and prod overrides.
    launchagents.mkdir(parents=True)
    (launchagents / "com.dalton.pg-backup.plist").write_text("<plist/>", encoding="utf-8")
    (launchagents / "com.apple.other.plist").write_text("<plist/>", encoding="utf-8")

    backup_scripts.mkdir(parents=True)
    (backup_scripts / "backup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (backup_scripts / "prune_backups.py").write_text("print('ok')\n", encoding="utf-8")
    (backup_scripts / "dalton-pg-20261113-030000.dump").write_text("pg", encoding="utf-8")

    (colima / "_lima" / "_disks").mkdir(parents=True)
    (colima / "_lima" / "_disks" / "datadisk").write_text("huge", encoding="utf-8")
    (colima / "default").mkdir(parents=True)
    (colima / "default" / "colima.yaml").write_text("cpu: 4\n", encoding="utf-8")

    prod_checkout.mkdir(parents=True, exist_ok=True)
    (prod_checkout / ".env").write_text("DB_PASSWORD=supersecret\n", encoding="utf-8")
    (prod_checkout / "agent_config.dalton.production.yaml").write_text(
        "sandbox:\n  provider: docker\n", encoding="utf-8"
    )

    sources = [
        backup_lib.SyncSourceSpec(
            name="var_dalton",
            src_dir=prod_var,
            dest_subdir="var_dalton",
            excludes=backup_lib.parse_pattern_list(
                "openclaw-data/temp,openclaw-data/research-outputs,pr_baseline,tmp,.dalton-runs,backups,*.dump,*.sqlite"
            ),
        ),
        backup_lib.SyncSourceSpec(
            name="dot_config_dalton",
            src_dir=user_cfg,
            dest_subdir="dot_config_dalton",
        ),
        backup_lib.SyncSourceSpec(
            name="launch_agents",
            src_dir=launchagents,
            dest_subdir="launch_agents",
            include_globs=("com.dalton.*.plist",),
            top_level_only=True,
        ),
        backup_lib.SyncSourceSpec(
            name="backup_scripts",
            src_dir=backup_scripts,
            dest_subdir="backup_scripts",
            include_globs=("*.sh", "*.py"),
            top_level_only=True,
        ),
        backup_lib.SyncSourceSpec(
            name="colima",
            src_dir=colima,
            dest_subdir="colima",
            excludes=("_disks", "_lima/_disks", "*.qcow2"),
        ),
        backup_lib.SyncSourceSpec(
            name="prod_overrides",
            src_dir=prod_checkout,
            dest_subdir="prod_overrides",
            include_globs=(".env", "agent_config.dalton*.yaml"),
            top_level_only=True,
        ),
    ]

    encrypt_calls: list[str] = []

    def _fake_encrypt_impl(src: Path, dst: Path, kf: Path) -> None:
        encrypt_calls.append(f"{src.name}->{dst.name}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"ENC:" + src.read_bytes())

    fake_encrypt = create_autospec(
        backup_lib.openssl_encrypt_file, side_effect=_fake_encrypt_impl
    )
    logs: list[str] = []

    stage1 = tmp_path / "stage1"
    s1 = backup_lib.stage_config_repo(
        sources=sources,
        stage_dir=stage1,
        existing_repo_dir=repo_dir,
        key_file=key_file,
        max_file_bytes=1024,
        encrypt_fn=fake_encrypt,
        log_fn=logs.append,
    )

    assert s1.plaintext_count == 3  # plist + backup.sh + prune_backups.py (var_dalton files are always encrypted)
    assert s1.encrypted_new == 9  # runtime_state.yaml + notes.txt + channel-gateway.env + conversation-archive-state.json + colima.yaml + .env + agent_config
    assert s1.encrypted_reused == 0
    assert s1.skipped_large == ("var_dalton:oversized.bin",)
    assert any("SKIP_LARGE var_dalton/oversized.bin" in ln for ln in logs)
    assert s1.skipped_forbidden == ("dot_config_dalton:backup.key",)

    assert (stage1 / "var_dalton" / "README.md.enc").is_file()
    assert (stage1 / "var_dalton" / "deps.lock.enc").is_file()
    assert (stage1 / "var_dalton" / "notes.txt.enc").is_file()
    assert not (stage1 / "var_dalton" / "notes.txt").exists()
    assert (stage1 / "var_dalton" / "runtime_state.yaml.enc").is_file()
    assert not (stage1 / "var_dalton" / "runtime_state.yaml").exists()
    assert not (stage1 / "var_dalton" / "state.sqlite").exists()
    assert not (stage1 / "var_dalton" / "pg.dump").exists()
    assert not (stage1 / "var_dalton" / "oversized.bin").exists()
    assert not (stage1 / "dot_config_dalton" / "backup.key").exists()
    assert (stage1 / "dot_config_dalton" / "channel-gateway.env.enc").is_file()
    assert not (stage1 / "dot_config_dalton" / "channel-gateway.env").exists()
    assert (stage1 / "dot_config_dalton" / "conversation-archive-state.json.enc").is_file()
    assert not (stage1 / "dot_config_dalton" / "conversation-archive-state.json").exists()
    assert (stage1 / "launch_agents" / "com.dalton.pg-backup.plist").is_file()
    assert not (stage1 / "launch_agents" / "com.apple.other.plist").exists()
    assert (stage1 / "backup_scripts" / "backup.sh").is_file()
    assert (stage1 / "backup_scripts" / "prune_backups.py").is_file()
    assert not (stage1 / "backup_scripts" / "dalton-pg-20261113-030000.dump").exists()
    assert (stage1 / "colima" / "default" / "colima.yaml.enc").is_file()
    assert not (stage1 / "colima" / "default" / "colima.yaml").exists()
    assert not (stage1 / "colima" / "_lima" / "_disks" / "datadisk").exists()
    assert (stage1 / "prod_overrides" / ".env.enc").is_file()
    assert (stage1 / "prod_overrides" / "agent_config.dalton.production.yaml.enc").is_file()
    assert not (stage1 / "prod_overrides" / "agent_config.dalton.production.yaml").exists()

    # Manifest records key_fingerprint (16 hex) and must not leak secret contents or backup key.
    manifest_text = (stage1 / backup_lib.MANIFEST_FILENAME).read_text(encoding="utf-8")
    manifest_data = json.loads(manifest_text)
    assert manifest_data["key_fingerprint"] == backup_lib.key_fingerprint(key_file)
    assert len(manifest_data["key_fingerprint"]) == 16
    assert "supersecret" not in manifest_text
    assert "abc123456789" not in manifest_text
    assert "secret-passphrase" not in manifest_text

    # Copy stage1 into repo_dir, then run stage2 with no changes -> zero re-encryptions!
    shutil.copytree(stage1, repo_dir, dirs_exist_ok=True)
    fake_encrypt.reset_mock()
    stage2 = tmp_path / "stage2"
    s2 = backup_lib.stage_config_repo(
        sources=sources,
        stage_dir=stage2,
        existing_repo_dir=repo_dir,
        key_file=key_file,
        max_file_bytes=1024,
        encrypt_fn=fake_encrypt,
    )
    assert s2.encrypted_reused == 9
    assert s2.encrypted_new == 0
    assert fake_encrypt.call_count == 0

    # Change one encrypted file -> only that one re-encrypts.
    (user_cfg / "channel-gateway.env").write_text(
        "INTERNAL_SERVICE_TOKEN=rotated987654\n", encoding="utf-8"
    )
    fake_encrypt.reset_mock()
    stage3 = tmp_path / "stage3"
    s3 = backup_lib.stage_config_repo(
        sources=sources,
        stage_dir=stage3,
        existing_repo_dir=repo_dir,
        key_file=key_file,
        max_file_bytes=1024,
        encrypt_fn=fake_encrypt,
    )
    assert s3.encrypted_reused == 8
    assert s3.encrypted_new == 1
    assert fake_encrypt.call_count == 1


def test_config_git_backup_subprocess_e2e_no_churn_plaintext_abort_and_push_retry(
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    bare_remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare_remote)],
        check=True,
        capture_output=True,
        text=True,
    )

    repo_dir = tmp_path / "dalton-config-repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo_dir, "config", "user.name", "Dalton Test")
    _git(repo_dir, "config", "user.email", "dalton-test@example.com")
    _git(repo_dir, "remote", "add", "origin", str(bare_remote))

    prod_checkout = tmp_path / "prod_checkout"
    prod_var = prod_checkout / "var" / "dalton"
    prod_var.mkdir(parents=True)
    (prod_var / "state.json").write_text('{"ok": true}\n', encoding="utf-8")
    (prod_checkout / ".env").write_text("POSTGRES_PASSWORD=pgsecret\n", encoding="utf-8")

    user_cfg = home_dir / ".config" / "dalton"
    user_cfg.mkdir(parents=True)
    (user_cfg / "settings.json").write_text('{"theme": "dark"}\n', encoding="utf-8")

    launchagents = home_dir / "Library" / "LaunchAgents"
    launchagents.mkdir(parents=True)
    backup_scripts = home_dir / "Backups" / "dalton"
    backup_scripts.mkdir(parents=True)
    colima = home_dir / ".colima"
    colima.mkdir(parents=True)

    key_file = user_cfg / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    log_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup.log"
    env_file = tmp_path / "offsite-backup.env"

    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=repo_dir,
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=prod_checkout,
        prod_var=prod_var,
        user_config=user_cfg,
        launchagents=launchagents,
        backup_scripts=backup_scripts,
        colima=colima,
    )

    script = OPS_DIR / "dalton_config_git_backup.sh"
    sub_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
        "DALTON_REPO_PYTHON": sys.executable,
    }

    # First run: generates 0600 backup.key, encrypts .env, commits, pushes to bare remote.
    res1 = subprocess.run(
        ["bash", str(script)], env=sub_env, capture_output=True, text=True, check=False
    )
    assert res1.returncode == 0, res1.stderr
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    enc_env = repo_dir / "prod_overrides" / ".env.enc"
    assert enc_env.is_file()
    assert not (repo_dir / "prod_overrides" / ".env").exists()
    assert not (repo_dir / "dot_config_dalton" / "backup.key").exists()

    # Verify real openssl decryption works with backup.key.
    dec = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-in",
            str(enc_env),
            "-pass",
            f"file:{key_file}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert dec.stdout == "POSTGRES_PASSWORD=pgsecret\n"

    commit_msg = _git(repo_dir, "log", "-1", "--pretty=%s").stdout.strip()
    assert commit_msg.startswith("config backup ")
    assert "个文件变化" in commit_msg
    commits_after_1 = int(_git(repo_dir, "rev-list", "--count", "HEAD").stdout.strip())
    enc_bytes_1 = enc_env.read_bytes()

    # Second run with no changes: reuses .enc and creates NO new commit.
    res2 = subprocess.run(
        ["bash", str(script)], env=sub_env, capture_output=True, text=True, check=False
    )
    assert res2.returncode == 0, res2.stderr
    assert enc_env.read_bytes() == enc_bytes_1
    commits_after_2 = int(_git(repo_dir, "rev-list", "--count", "HEAD").stdout.strip())
    assert commits_after_2 == commits_after_1

    # Inject a stray plaintext secret inside repo_dir -> pre-commit scan must abort & alert.
    (repo_dir / "stray.txt").write_text("MY_API_KEY=leaked_secret_value\n", encoding="utf-8")
    res_leak = subprocess.run(
        ["bash", str(script)], env=sub_env, capture_output=True, text=True, check=False
    )
    assert res_leak.returncode != 0
    assert int(_git(repo_dir, "rev-list", "--count", "HEAD").stdout.strip()) == commits_after_1
    assert "PLAINTEXT_SECRET_DETECTED" in alerts_file.read_text(encoding="utf-8")
    (repo_dir / "stray.txt").unlink()

    # Point origin to a broken URL + change a file -> push retries & alerts, local commit preserved.
    _git(repo_dir, "remote", "set-url", "origin", str(tmp_path / "missing-remote.git"))
    (prod_var / "state.json").write_text('{"ok": false}\n', encoding="utf-8")
    res_push_fail = subprocess.run(
        ["bash", str(script)], env=sub_env, capture_output=True, text=True, check=False
    )
    assert res_push_fail.returncode != 0
    assert int(_git(repo_dir, "rev-list", "--count", "HEAD").stdout.strip()) == commits_after_1 + 1
    assert "GIT_PUSH_FAILED" in alerts_file.read_text(encoding="utf-8")


def test_verify_external_mount_and_unmounted_script_refusal(tmp_path: Path) -> None:
    mount_root = tmp_path / "EveSSD"
    mount_root.mkdir()
    target = mount_root / "Dalton" / "backups" / "weekly"

    # Unmounted directory on internal disk must be rejected.
    mock_unmounted = create_autospec(os.path.ismount, return_value=False)
    with pytest.raises(backup_lib.MountCheckError, match="not mounted"):
        backup_lib.verify_external_mount(mount_root, target, is_mount_fn=mock_unmounted)

    # Mounted volume rejects targets outside mount_root.
    mock_mounted = create_autospec(os.path.ismount, return_value=True)
    backup_lib.verify_external_mount(mount_root, target, is_mount_fn=mock_mounted)
    with pytest.raises(backup_lib.MountCheckError, match="outside external mount"):
        backup_lib.verify_external_mount(
            mount_root, tmp_path / "internal" / "weekly", is_mount_fn=mock_mounted
        )

    # Subprocess test against real dalton_data_backup.sh with unmounted tmp_path dir.
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    alerts_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    log_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup.log"
    env_file = tmp_path / "offsite-backup.env"
    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=tmp_path / "repo",
        key_file=tmp_path / "backup.key",
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=tmp_path / "prod",
        prod_var=tmp_path / "prod" / "var",
        user_config=tmp_path / "cfg",
        launchagents=tmp_path / "la",
        backup_scripts=tmp_path / "bs",
        colima=tmp_path / "colima",
        ext_mount=mount_root,
    )
    res = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home_dir),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
            "DALTON_REPO_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert not (mount_root / "Dalton").exists()
    assert "EXTERNAL_DRIVE_UNMOUNTED" in alerts_file.read_text(encoding="utf-8")


def test_weekly_retention_pruning_local_and_drive(tmp_path: Path) -> None:
    weekly_root = tmp_path / "weekly"
    weekly_root.mkdir()

    dates = [f"2026-09-{day:02d}" for day in range(1, 11)]  # 10 valid weekly dirs
    for d in dates:
        (weekly_root / d).mkdir()
        (weekly_root / d / "marker.txt").write_text(d, encoding="utf-8")

    # Non-matching entries must never be deleted.
    (weekly_root / "manual-backup").mkdir()
    (weekly_root / ".tmp").mkdir()
    (weekly_root / "2026-99-99").mkdir()
    (weekly_root / "2020-01-02").write_text("file-not-dir", encoding="utf-8")
    outside = tmp_path / "outside-keep"
    outside.mkdir()
    (weekly_root / "2020-01-01").symlink_to(outside)

    deleted = backup_lib.prune_local_weekly_dirs(weekly_root, retain_weeks=8)
    assert [p.name for p in deleted] == ["2026-09-01", "2026-09-02"]
    for kept in dates[2:]:
        assert (weekly_root / kept).is_dir()
    assert (weekly_root / "manual-backup").is_dir()
    assert (weekly_root / ".tmp").is_dir()
    assert (weekly_root / "2026-99-99").is_dir()
    assert (weekly_root / "2020-01-02").is_file()
    assert (weekly_root / "2020-01-01").is_symlink()
    assert outside.is_dir()

    # Drive pruning via `select-prune-names` CLI subprocess (handles trailing slash from rclone lsf).
    lsf_input = "\n".join(
        [
            "2026-10-01/",
            "2026-10-08/",
            "2026-10-15/",
            "2026-10-22/",
            "2026-10-29/",
            "2026-11-05/",
            "manual-archive/",
            "notes/",
        ]
    )
    proc = subprocess.run(
        [sys.executable, str(OPS_DIR / "backup_lib.py"), "select-prune-names", "--retain", "4"],
        input=lsf_input,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.splitlines() == ["2026-10-01", "2026-10-08"]


def test_rclone_remote_detection_skip_when_unconfigured() -> None:
    missing_which = create_autospec(shutil.which, return_value=None)
    ok1, reason1 = backup_lib.check_rclone_remote("gdrive", which_fn=missing_which)
    assert ok1 is False
    assert "not found" in reason1

    present_which = create_autospec(shutil.which, return_value="/opt/homebrew/bin/rclone")
    run_without_remote = create_autospec(
        subprocess.run,
        return_value=subprocess.CompletedProcess(
            args=["rclone", "listremotes"], returncode=0, stdout="onedrive:\n", stderr=""
        ),
    )
    ok2, reason2 = backup_lib.check_rclone_remote(
        "gdrive", which_fn=present_which, run_fn=run_without_remote
    )
    assert ok2 is False
    assert "not in listremotes" in reason2

    run_with_remote = create_autospec(
        subprocess.run,
        return_value=subprocess.CompletedProcess(
            args=["rclone", "listremotes"],
            returncode=0,
            stdout="onedrive:\ngdrive:\n",
            stderr="",
        ),
    )
    ok3, _ = backup_lib.check_rclone_remote(
        "gdrive", which_fn=present_which, run_fn=run_with_remote
    )
    assert ok3 is True


def test_stream_cap_and_latest_pg_dump(tmp_path: Path) -> None:
    out_ok = tmp_path / "ok.tar.gz"
    ok, total = backup_lib.stream_copy_with_limit(
        io.BytesIO(b"hello world"), out_ok, max_bytes=64, gzip_output=True
    )
    assert ok is True
    assert total == 11
    with gzip.open(out_ok, "rb") as fh:
        assert fh.read() == b"hello world"

    # Subprocess stream-cap exits 42 and removes partial output when over max_bytes.
    out_big = tmp_path / "big.tar.gz"
    proc = subprocess.run(
        [
            sys.executable,
            str(OPS_DIR / "backup_lib.py"),
            "stream-cap",
            "--max-bytes",
            "10",
            "--gzip",
            "--out",
            str(out_big),
        ],
        input=b"0123456789abcdef",
        capture_output=True,
        check=False,
    )
    assert proc.returncode == backup_lib.STREAM_CAP_EXIT_CODE
    assert not out_big.exists()

    # Latest pg dump ignores .tmp, empty dumps, symlinks, and non-regular names.
    pg_dir = tmp_path / "pg"
    pg_dir.mkdir()
    (pg_dir / "dalton-pg-20261110-030000.dump").write_bytes(b"old")
    latest = pg_dir / "dalton-pg-20261112-030000.dump"
    latest.write_bytes(b"latest-ok")
    (pg_dir / "dalton-pg-20261113-030000.dump.tmp").write_bytes(b"partial")
    (pg_dir / "dalton-pg-20261114-030000.dump").write_bytes(b"")
    (pg_dir / "pre-deploy.dump").write_bytes(b"manual")
    (pg_dir / "dalton-pg-20261115-030000.dump").symlink_to(latest)
    assert backup_lib.find_latest_pg_dump(pg_dir) == latest


def test_alert_line_format_and_json_config_guard(tmp_path: Path) -> None:
    ts = dt.datetime(2026, 11, 13, 19, 30, 0, tzinfo=dt.timezone.utc)
    line = backup_lib.format_alert_line(
        level="error",
        source="dalton_data_backup",
        code="external drive unmounted",
        message="line1\nline2",
        timestamp=ts,
    )
    assert line == (
        "2026-11-13T19:30:00+00:00 [ERROR] [dalton_data_backup] "
        "[EXTERNAL_DRIVE_UNMOUNTED] line1 line2"
    )
    assert "\n" not in line

    # If DALTON_OPS_ALERTS_FILE points to channel-gateway's JSON config, never corrupt it.
    json_cfg = tmp_path / "ops-alerts.json"
    original_json = '{"targets":["feishu:user:ou_1"],"messages":{"backend_down":"down","backend_recovered":"up"}}\n'
    json_cfg.write_text(original_json, encoding="utf-8")
    gw_env = tmp_path / "channel-gateway.env"
    gw_env.write_text(f'DALTON_OPS_ALERTS_FILE="{json_cfg}"\n', encoding="utf-8")
    fallback = tmp_path / "backup-alerts.log"

    proc = subprocess.run(
        [
            sys.executable,
            str(OPS_DIR / "backup_lib.py"),
            "emit-alert",
            "--level",
            "ERROR",
            "--source",
            "dalton_config_git_backup",
            "--code",
            "GIT_PUSH_FAILED",
            "--message",
            "push failed after retry",
            "--gateway-env-file",
            str(gw_env),
            "--default-file",
            str(fallback),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == str(fallback)
    assert "Refusing to append log line to JSON config" in proc.stderr
    assert json_cfg.read_text(encoding="utf-8") == original_json
    written = fallback.read_text(encoding="utf-8").splitlines()
    assert len(written) == 1
    assert "[ERROR] [dalton_config_git_backup] [GIT_PUSH_FAILED] push failed after retry" in written[0]

    # When DALTON_OPS_ALERTS_FILE is a line-oriented log file, append directly to it.
    log_target = tmp_path / "ops-alerts.log"
    target, warning = backup_lib.append_alert(
        level="WARN",
        source="dalton_data_backup",
        code="RCLONE_NOT_CONFIGURED",
        message="skipped upload",
        ops_alerts_file=str(log_target),
        default_alerts_file=fallback,
    )
    assert warning is None
    assert target == log_target
    assert "[WARN] [dalton_data_backup] [RCLONE_NOT_CONFIGURED] skipped upload" in log_target.read_text(
        encoding="utf-8"
    )


def test_rclone_noproxy_command_prefix_and_env_clearing(tmp_path: Path) -> None:
    cmd = backup_lib.build_rclone_cmd("copy", "/src", "gdrive:DaltonBackup/db")
    expected_prefix = [
        "env",
        "-u",
        "http_proxy",
        "-u",
        "https_proxy",
        "-u",
        "HTTP_PROXY",
        "-u",
        "HTTPS_PROXY",
        "-u",
        "all_proxy",
        "-u",
        "ALL_PROXY",
        "-u",
        "no_proxy",
        "-u",
        "NO_PROXY",
        "rclone",
    ]
    assert cmd[: len(expected_prefix)] == expected_prefix
    assert cmd[len(expected_prefix) :] == ["copy", "/src", "gdrive:DaltonBackup/db"]

    present_which = create_autospec(shutil.which, return_value="/opt/homebrew/bin/rclone")
    captured_cmds: list[list[str]] = []

    def _capture_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_cmds.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="gdrive:\n", stderr=""
        )

    run_mock = create_autospec(subprocess.run, side_effect=_capture_run)
    ok, _ = backup_lib.check_rclone_remote(
        "gdrive", which_fn=present_which, run_fn=run_mock
    )
    assert ok is True
    assert captured_cmds == [[*expected_prefix, "listremotes"]]

    # Verify bash `rclone_noproxy` actually unsets all proxy env vars before invoking rclone.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    env_dump = tmp_path / "rclone_env.txt"
    fake_rclone = fake_bin / "rclone"
    fake_rclone.write_text(
        f"""#!/usr/bin/env python3
import os, pathlib, sys
keys = {list(backup_lib.PROXY_ENV_VARS)!r}
lines = [f"{{k}}={{os.environ.get(k, '<UNSET>')}}" for k in keys]
pathlib.Path({str(env_dump)!r}).write_text("\\n".join(lines) + "\\n")
""",
        encoding="utf-8",
    )
    fake_rclone.chmod(0o755)

    sub_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        **{k: "http://127.0.0.1:7890" for k in backup_lib.PROXY_ENV_VARS},
    }
    subprocess.run(
        [
            "bash",
            "-c",
            f'source "{OPS_DIR / "_common.sh"}" && rclone_noproxy version',
        ],
        env=sub_env,
        check=True,
        capture_output=True,
        text=True,
    )
    dumped = env_dump.read_text(encoding="utf-8").splitlines()
    assert dumped == [f"{k}=<UNSET>" for k in backup_lib.PROXY_ENV_VARS]



def test_rclone_noproxy_sets_large_drive_chunk_size(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    dump = tmp_path / "chunk.txt"
    fake_rclone = fake_bin / "rclone"
    fake_rclone.write_text(
        f"""#!/usr/bin/env python3
import os, pathlib
pathlib.Path({str(dump)!r}).write_text(os.environ.get("RCLONE_DRIVE_CHUNK_SIZE", "<UNSET>"))
""",
        encoding="utf-8",
    )
    fake_rclone.chmod(0o755)
    base_env = {"PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(tmp_path)}
    script = f'source "{OPS_DIR / "_common.sh"}" && rclone_noproxy version'

    subprocess.run(["bash", "-c", script], env=base_env, check=True, capture_output=True, text=True)
    assert dump.read_text(encoding="utf-8") == "128M"

    subprocess.run(
        ["bash", "-c", script],
        env={**base_env, "RCLONE_DRIVE_CHUNK_SIZE": "32M"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert dump.read_text(encoding="utf-8") == "32M"

def test_db_offsite_helpers_and_weekly_excludes_postgres() -> None:
    assert backup_lib.expected_openssl_enc_size(0) == 32
    assert backup_lib.expected_openssl_enc_size(16) == 48
    assert backup_lib.expected_openssl_enc_size(31) == 48
    assert backup_lib.expected_openssl_enc_size(32) == 64

    assert backup_lib.parse_rclone_about_free_bytes('{"total": 1000, "free": 500}') == 500
    assert backup_lib.parse_rclone_about_free_bytes("{}") is None
    assert backup_lib.parse_rclone_about_free_bytes("not-json") is None

    ok_low, _ = backup_lib.check_drive_free_space(999, 500, multiplier=2)
    assert ok_low is False
    ok_exact, _ = backup_lib.check_drive_free_space(1000, 500, multiplier=2)
    assert ok_exact is True
    ok_unavail, _ = backup_lib.check_drive_free_space(None, 500, multiplier=2)
    assert ok_unavail is True

    lsjson_ok = json.dumps([{"Name": "dalton-pg-20261113-030000.dump.enc", "Size": 64}])
    assert backup_lib.verify_rclone_object_size(
        lsjson_ok, "dalton-pg-20261113-030000.dump.enc", 64
    )[0] is True
    assert backup_lib.verify_rclone_object_size(
        lsjson_ok, "dalton-pg-20261113-030000.dump.enc", 128
    )[0] is False

    md5_ok = "0123456789abcdef0123456789abcdef"
    assert backup_lib.verify_rclone_hashsum(
        f"{md5_ok}  dalton-pg-20261113-030000.dump.enc.partial-1234\n",
        "dalton-pg-20261113-030000.dump.enc.partial-1234",
        md5_ok,
    )[0] is True
    assert backup_lib.verify_rclone_hashsum(
        "fedcba9876543210fedcba9876543210  obj.enc\n", "obj.enc", md5_ok
    )[0] is False
    assert backup_lib.verify_rclone_hashsum(
        "UNSUPPORTED  obj.enc\n", "obj.enc", md5_ok
    )[0] is False

    # B3 weekly script streams directly via rclone rcat/moveto, excludes Postgres, and never uses --size-only.
    weekly_src = (OPS_DIR / "dalton_data_backup.sh").read_text(encoding="utf-8")
    assert "latest-pg-dump" not in weekly_src
    assert "pg-backup.ref" not in weekly_src
    assert "--size-only" not in weekly_src
    assert "rclone_noproxy rcat" in weekly_src
    assert "rclone_noproxy lsjson" in weekly_src
    assert "rclone_noproxy hashsum md5" in weekly_src
    assert "rclone_noproxy moveto" in weekly_src
    assert "rclone_noproxy deletefile" in weekly_src
    assert "rclone_noproxy lsf" in weekly_src
    assert "rclone_noproxy purge" in weekly_src


def _make_mounted_python_wrapper(tmp_path: Path, mount_root: Path) -> Path:
    """Test helper: simulate ``os.path.ismount(mount_root) == True`` without OS root mounts."""
    py_wrap = tmp_path / "mounted_python.py"
    py_wrap.write_text(
        f"""#!{sys.executable}
import os
import pathlib
import runpy
import sys

mount = pathlib.Path({str(mount_root)!r}).resolve()
orig_ismount = os.path.ismount
os.path.ismount = lambda p: pathlib.Path(p).resolve() == mount or orig_ismount(p)
target = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(target, run_name="__main__")
""",
        encoding="utf-8",
    )
    py_wrap.chmod(0o755)
    return py_wrap


def _write_fake_rclone(
    fake_bin: Path,
    *,
    drive_store: Path,
    about_file: Path,
    proxy_check_file: Path,
    rcat_sizes_log: Path,
) -> Path:
    fake_rclone = fake_bin / "rclone"
    fake_rclone.write_text(
        f"""#!/usr/bin/env python3
import hashlib, json, os, pathlib, shutil, sys

drive_root = pathlib.Path({str(drive_store)!r})
about_path = pathlib.Path({str(about_file)!r})
proxy_seen = pathlib.Path({str(proxy_check_file)!r})
sizes_log = pathlib.Path({str(rcat_sizes_log)!r})

for k in {list(backup_lib.PROXY_ENV_VARS)!r}:
    if k in os.environ:
        with proxy_seen.open("a") as fh:
            fh.write(f"{{k}}={{os.environ[k]}}\\n")

clean = []
rcat_size = None
skip_next = False
i = 1
argv = sys.argv[1:]
while i < len(sys.argv):
    tok = sys.argv[i]
    if tok in {{"--cache-dir", "--temp-dir", "--log-file", "--log-level"}}:
        i += 2
        continue
    if tok == "--size":
        rcat_size = sys.argv[i + 1]
        i += 2
        continue
    if tok.startswith("--size="):
        rcat_size = tok.split("=", 1)[1]
        i += 1
        continue
    if tok.startswith("--"):
        i += 1
        continue
    clean.append(tok)
    i += 1

cmd = clean[0] if clean else ""
if cmd == "listremotes":
    print("gdrive:")
elif cmd == "about":
    print(about_path.read_text())
elif cmd == "rcat":
    remote_spec = clean[1]
    rel = remote_spec.split(":", 1)[1].lstrip("/")
    dst = drive_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    fail_match = os.environ.get("FAKE_RCLONE_FAIL_MATCH", "")
    if fail_match and fail_match in rel:
        dst.write_bytes(b"partial")
        sys.stderr.write(f"simulated rcat failure for {{rel}}\\n")
        sys.exit(1)
    payload = sys.stdin.buffer.read()
    with sizes_log.open("a") as fh:
        fh.write(json.dumps({{"rel": rel, "size_flag": rcat_size, "actual": len(payload)}}) + "\\n")
    if os.environ.get("FAKE_RCLONE_FAIL") == "rcat":
        dst.write_bytes(payload[:8])
        sys.stderr.write("simulated rcat mid-stream failure\\n")
        sys.exit(1)
    if os.environ.get("FAKE_RCLONE_CORRUPT_REMOTE") == "1":
        dst.write_bytes(payload[:-1] + b"X")
        sys.exit(0)
    dst.write_bytes(payload)
elif cmd == "lsjson":
    remote_spec = clean[1]
    rel = remote_spec.split(":", 1)[1].lstrip("/")
    obj = drive_root / rel
    if not obj.is_file():
        print("[]")
    else:
        print(json.dumps([{{"Name": obj.name, "Size": obj.stat().st_size}}]))
elif cmd == "hashsum":
    # clean = ["hashsum", "md5", remote_spec]
    remote_spec = clean[2]
    rel = remote_spec.split(":", 1)[1].lstrip("/")
    obj = drive_root / rel
    if not obj.is_file():
        sys.exit(1)
    if os.environ.get("FAKE_RCLONE_BAD_HASH") == "1":
        print(f"00000000000000000000000000000000  {{obj.name}}")
    else:
        digest = hashlib.md5(obj.read_bytes()).hexdigest()
        print(f"{{digest}}  {{obj.name}}")
elif cmd == "moveto":
    src_rel = clean[1].split(":", 1)[1].lstrip("/")
    dst_rel = clean[2].split(":", 1)[1].lstrip("/")
    src_p = drive_root / src_rel
    dst_p = drive_root / dst_rel
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_p), str(dst_p))
elif cmd == "deletefile":
    rel = clean[1].split(":", 1)[1].lstrip("/")
    del_log = os.environ.get("FAKE_RCLONE_DELETE_LOG")
    if del_log:
        with pathlib.Path(del_log).open("a") as fh:
            fh.write(rel + "\\n")
    target = drive_root / rel
    if target.is_file():
        target.unlink()
elif cmd == "lsf":
    rel = clean[1].split(":", 1)[1].strip("/")
    base = drive_root / rel
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if child.is_dir():
                print(child.name + "/")
elif cmd == "purge":
    rel = clean[1].split(":", 1)[1].strip("/")
    target = drive_root / rel
    if target.is_dir():
        shutil.rmtree(target)
""",
        encoding="utf-8",
    )
    fake_rclone.chmod(0o755)
    return fake_rclone


def test_dalton_db_offsite_subprocess_mount_partial_hash_and_cleanup(
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    pg_dir = tmp_path / "pg_backups"
    fake_bin = tmp_path / "bin"
    drive_store = tmp_path / "drive_store"
    ext_mount = tmp_path / "ext_ssd"
    for d in (home_dir, pg_dir, fake_bin, drive_store, ext_mount):
        d.mkdir(parents=True)

    # Pre-seed 8 existing daily DB folders on fake Drive remote (should prune oldest to keep 7).
    existing_days = [f"2026-11-{day:02d}" for day in range(1, 9)]
    for day in existing_days:
        (drive_store / "DaltonBackup" / "db" / day).mkdir(parents=True)

    dump_payload = b"PGDMP-synthetic-postgres-dump-content-" * 100
    dump_file = pg_dir / "dalton-pg-20261113-030000.dump"
    dump_file.write_bytes(dump_payload)

    key_file = home_dir / ".config" / "dalton" / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    log_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup.log"
    env_file = tmp_path / "offsite-backup.env"
    about_file = tmp_path / "about.json"
    proxy_check_file = tmp_path / "proxy_seen.txt"
    rcat_sizes_log = tmp_path / "rcat_sizes.jsonl"

    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=tmp_path / "repo",
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=tmp_path / "prod",
        prod_var=tmp_path / "prod" / "var",
        user_config=home_dir / ".config" / "dalton",
        launchagents=home_dir / "Library" / "LaunchAgents",
        backup_scripts=pg_dir,
        colima=home_dir / ".colima",
        ext_mount=ext_mount,
    )
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f'DALTON_PG_BACKUP_DIR="{pg_dir}"\n')
        fh.write('DALTON_RCLONE_REMOTE="gdrive"\n')
        fh.write('DALTON_RCLONE_DRIVE_DIR="DaltonBackup"\n')
        fh.write('DALTON_DB_RETAIN_COUNT="7"\n')

    _write_fake_rclone(
        fake_bin,
        drive_store=drive_store,
        about_file=about_file,
        proxy_check_file=proxy_check_file,
        rcat_sizes_log=rcat_sizes_log,
    )

    # Case 0 (R3): With REAL sys.executable and unmounted ext_mount, DB script MUST fail and NOT fall back to /tmp.
    res_unmounted = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_db_offsite.sh")],
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(home_dir),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
            "DALTON_REPO_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_unmounted.returncode != 0
    assert "EXTERNAL_DRIVE_UNMOUNTED" in alerts_file.read_text(encoding="utf-8")
    assert not rcat_sizes_log.exists()

    mounted_py = _make_mounted_python_wrapper(tmp_path, ext_mount)
    sub_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home_dir),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
        "DALTON_REPO_PYTHON": str(mounted_py),
        "http_proxy": "http://127.0.0.1:7890",
        "https_proxy": "http://127.0.0.1:7890",
        "ALL_PROXY": "socks5://127.0.0.1:7891",
    }

    # Case 1: Drive free space < 2x backup size -> skip upload and emit DRIVE_LOW_SPACE alert.
    about_file.write_text('{"free": 100}\n', encoding="utf-8")
    res_low = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_db_offsite.sh")],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_low.returncode != 0
    assert "DRIVE_LOW_SPACE" in alerts_file.read_text(encoding="utf-8")
    today_str = dt.datetime.now().strftime("%Y-%m-%d")
    day_dir = drive_store / "DaltonBackup" / "db" / today_str
    assert not (day_dir / f"{dump_file.name}.enc").exists()

    # Case 2: Mid-stream rcat failure -> alert emitted AND partial object deleted from remote.
    about_file.write_text('{"free": 100000000}\n', encoding="utf-8")
    res_fail = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_db_offsite.sh")],
        env={**sub_env, "FAKE_RCLONE_FAIL": "rcat"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_fail.returncode != 0
    assert "DB_UPLOAD_FAILED" in alerts_file.read_text(encoding="utf-8")
    assert not (day_dir / f"{dump_file.name}.enc").exists()
    assert list(day_dir.glob("*.partial-*")) == []

    # Case 3: Hash mismatch during verify -> DB_HASH_MISMATCH alert AND partial object deleted.
    res_bad_hash = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_db_offsite.sh")],
        env={**sub_env, "FAKE_RCLONE_BAD_HASH": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_bad_hash.returncode != 0
    assert "DB_HASH_MISMATCH" in alerts_file.read_text(encoding="utf-8")
    assert not (day_dir / f"{dump_file.name}.enc").exists()
    assert list(day_dir.glob("*.partial-*")) == []

    # Case 4: Sufficient free space -> rcat --size to .partial-, lsjson + md5 match, moveto final .enc, prune to 7.
    rcat_sizes_log.unlink(missing_ok=True)
    res_ok = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_db_offsite.sh")],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_ok.returncode == 0, res_ok.stderr
    assert not proxy_check_file.exists()

    size_records = [
        json.loads(line) for line in rcat_sizes_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(size_records) == 1
    expected_sz = backup_lib.expected_openssl_enc_size(len(dump_payload))
    assert ".partial-" in size_records[0]["rel"]
    assert size_records[0]["size_flag"] == str(expected_sz)
    assert size_records[0]["actual"] == expected_sz

    assert list(day_dir.glob("*.partial-*")) == []
    uploaded = day_dir / f"{dump_file.name}.enc"
    assert uploaded.is_file()
    dec = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-in",
            str(uploaded),
            "-pass",
            f"file:{key_file}",
        ],
        capture_output=True,
        check=True,
    )
    assert dec.stdout == dump_payload

    remaining_db_dirs = sorted(
        p.name for p in (drive_store / "DaltonBackup" / "db").iterdir() if p.is_dir()
    )
    assert len(remaining_db_dirs) == 7
    assert "2026-11-01" not in remaining_db_dirs
    assert "2026-11-02" not in remaining_db_dirs
    assert today_str in remaining_db_dirs


def test_gateway_alert_payload_and_emit_alert_curl_hook(tmp_path: Path) -> None:
    payload_str = backup_lib.format_gateway_alert_payload(
        source="dalton_db_offsite",
        code="DRIVE_LOW_SPACE",
        message="line1\nline2\t" + ("x" * 350),
    )
    parsed = json.loads(payload_str)
    assert parsed["code"] == "backup_failed"
    assert len(parsed["detail"]) == 300
    assert "\n" not in parsed["detail"]
    assert "\t" not in parsed["detail"]

    # Subprocess emit_alert with fake curl: logs first, sends X-Service-Token via -K - stdin, never leaks token.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_capture = tmp_path / "curl_capture.json"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        f"""#!/usr/bin/env python3
import json, os, pathlib, sys
stdin_data = sys.stdin.read()
pathlib.Path({str(curl_capture)!r}).write_text(
    json.dumps({{"argv": sys.argv[1:], "stdin": stdin_data}})
)
sys.exit(int(os.environ.get("FAKE_CURL_RC", "0")))
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    home_dir = tmp_path / "home"
    home_dir.mkdir()
    alerts_file = tmp_path / "backup-alerts.log"
    log_file = tmp_path / "backup.log"
    secret_token = "tok_super_secret_gateway_1234567890"
    env_file = tmp_path / "offsite-backup.env"
    env_file.write_text(
        "\n".join(
            [
                f'DALTON_OFFSITE_BACKUP_LOG_FILE="{log_file}"',
                f'DALTON_BACKUP_ALERTS_FILE="{alerts_file}"',
                'DALTON_CHANNEL_GATEWAY_URL="http://127.0.0.1:8962"',
                f'INTERNAL_SERVICE_TOKEN="{secret_token}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sub_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home_dir),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
        "DALTON_REPO_PYTHON": sys.executable,
    }
    res = subprocess.run(
        [
            "bash",
            "-c",
            f'SCRIPT_NAME=dalton_db_offsite; source "{OPS_DIR / "_common.sh"}" && load_offsite_backup_env && emit_alert ERROR DB_UPLOAD_FAILED "上传超时"',
        ],
        env=sub_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert secret_token not in res.stdout
    assert secret_token not in res.stderr
    assert secret_token not in log_file.read_text(encoding="utf-8")
    assert "[ERROR] [dalton_db_offsite] [DB_UPLOAD_FAILED] 上传超时" in alerts_file.read_text(
        encoding="utf-8"
    )

    captured = json.loads(curl_capture.read_text(encoding="utf-8"))
    assert "http://127.0.0.1:8962/internal/ops-alerts" in captured["argv"]
    assert all(secret_token not in arg for arg in captured["argv"])
    assert f'header = "X-Service-Token: {secret_token}"' in captured["stdin"]
    d_idx = captured["argv"].index("-d")
    sent_json = json.loads(captured["argv"][d_idx + 1])
    assert sent_json == {
        "code": "backup_failed",
        "detail": "dalton_db_offsite: DB_UPLOAD_FAILED - 上传超时",
    }

    # Even if curl fails (gateway unreachable), emit_alert must not crash or fail caller.
    sub_env["FAKE_CURL_RC"] = "7"
    res_down = subprocess.run(
        [
            "bash",
            "-c",
            f'SCRIPT_NAME=dalton_db_offsite; source "{OPS_DIR / "_common.sh"}" && load_offsite_backup_env && emit_alert ERROR DB_UPLOAD_FAILED "网关断开"',
        ],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_down.returncode == 0
    assert "网关断开" in alerts_file.read_text(encoding="utf-8")


def test_secret_detection_binary_unverified_enc_structured_and_keywords(
    tmp_path: Path,
) -> None:
    # 1. NUL bytes and non-UTF-8 / UTF-16LE bytes must be flagged for encryption.
    assert backup_lib.detect_secret_reason("keystore.dat", b"\x00\x01binary-secret") is not None
    assert (
        backup_lib.detect_secret_reason(
            "config.ini", "API_KEY=sk-utf16\n".encode("utf-16-le")
        )
        is not None
    )

    # 2. JSON numeric, YAML inline map, .netrc single/multiline, expanded keywords, and Codex R2 multiline/quoted-# cases.
    assert backup_lib.detect_secret_reason("cfg.json", b'{"pin_secret": 987654}') is not None
    assert backup_lib.detect_secret_reason("cfg.json", b'{"password":\n 987654}') is not None
    assert backup_lib.detect_secret_reason("cfg.json", b'{"password":\n "actual-secret"}') is not None
    assert (
        backup_lib.detect_secret_reason("cfg.yaml", b'auth:\n  password:\n    "actual-secret"\n')
        is not None
    )
    assert backup_lib.detect_secret_reason("env.sh", b'PASSWORD="#actual-secret"\n') is not None
    assert (
        backup_lib.detect_secret_reason("cfg.yaml", b'auth: { api_key: "sk-inline-1" }')
        is not None
    )
    assert (
        backup_lib.detect_secret_reason(
            "creds.txt", b"machine api.github.com login alice password s3cr3tvalue"
        )
        is not None
    )
    assert (
        backup_lib.detect_secret_reason("auth.conf", b"  passphrase my-unlock-phrase\n")
        is not None
    )
    for sample in (
        b"MY_CREDENTIAL=cred-123\n",
        b"DB_PASSWD=hunter2\n",
        b"SSH_PASSPHRASE=unlockme\n",
        b"SESSION_COOKIE=sess=xyz\n",
        b"OAUTH_BEARER=br-999\n",
    ):
        assert backup_lib.detect_secret_reason("service.conf", sample) is not None
    assert backup_lib.detect_secret_reason("oauth_bearer.txt", b"placeholder") is not None

    # Empty JSON null or blank values stay plaintext.
    assert backup_lib.detect_secret_reason("clean.json", b'{"pin_secret": null}') is None

    # 3. Unverified .enc files (missing from manifest OR modified ciphertext) fail scan_plaintext_tree.
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "rogue.enc").write_bytes(b"PLAINTEXT_BYPASS=1")
    findings_rogue = backup_lib.scan_plaintext_tree(repo_dir)
    assert findings_rogue == [("rogue.enc", "unverified_enc_not_in_manifest")]

    # Valid manifest-verified .enc passes; tampering triggers hash mismatch.
    good_enc = repo_dir / "valid.enc"
    good_enc.write_bytes(b"Salted__ciphertext")
    manifest = {
        "version": 1,
        "entries": {
            "valid.enc": {
                "plaintext_sha256": "a" * 64,
                "ciphertext_sha256": backup_lib.sha256_file(good_enc),
                "size": 10,
            }
        },
    }
    (repo_dir / backup_lib.MANIFEST_FILENAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (repo_dir / "rogue.enc").unlink()
    assert backup_lib.scan_plaintext_tree(repo_dir) == []

    good_enc.write_bytes(b"X")
    findings_tampered = backup_lib.scan_plaintext_tree(repo_dir)
    assert findings_tampered == [("valid.enc", "unverified_enc_hash_mismatch")]


def test_ciphertext_reuse_integrity_and_single_read_consistency(
    tmp_path: Path,
) -> None:
    src_dir = tmp_path / "src"
    repo_dir = tmp_path / "repo"
    stage1 = tmp_path / "stage1"
    stage2 = tmp_path / "stage2"
    src_dir.mkdir()
    repo_dir.mkdir()
    key_file = tmp_path / "backup.key"
    key_file.write_text("unit-test-passphrase\n", encoding="utf-8")

    secret_src = src_dir / "service.env"
    secret_src.write_text("TOKEN=initial-value-1\n", encoding="utf-8")

    spec = backup_lib.SyncSourceSpec(name="svc", src_dir=src_dir, dest_subdir="svc")

    # Mutate src_file on disk *after* stage_config_repo reads it (via encrypt_fn callback)
    # to confirm stage_config_repo hashes and encrypts the exact single-read bytes (never re-opens src_file).
    def _encrypt_with_concurrent_src_mutation(src: Path, dst: Path, kf: Path) -> None:
        observed = src.read_bytes()
        # Simulate external writer racing after initial read.
        secret_src.write_text("TOKEN=raced-mutated-2\n", encoding="utf-8")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"ENC:" + observed)

    s1 = backup_lib.stage_config_repo(
        sources=[spec],
        stage_dir=stage1,
        existing_repo_dir=repo_dir,
        key_file=key_file,
        max_file_bytes=1024 * 1024,
        encrypt_fn=_encrypt_with_concurrent_src_mutation,
    )
    assert s1.encrypted_new == 1
    manifest1 = json.loads((stage1 / backup_lib.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    entry1 = manifest1["entries"]["svc/service.env.enc"]
    assert entry1["plaintext_sha256"] == backup_lib.sha256_bytes(b"TOKEN=initial-value-1\n")
    assert (stage1 / "svc" / "service.env.enc").read_bytes() == b"ENC:TOKEN=initial-value-1\n"

    # Reset source to match initial value, copy stage1 to repo, then corrupt .enc to 1 byte.
    secret_src.write_text("TOKEN=initial-value-1\n", encoding="utf-8")
    shutil.copytree(stage1, repo_dir, dirs_exist_ok=True)
    (repo_dir / "svc" / "service.env.enc").write_bytes(b"X")

    fake_encrypt = create_autospec(
        backup_lib.openssl_encrypt_file,
        side_effect=lambda s, d, k: d.write_bytes(b"REENC:" + s.read_bytes()),
    )
    s2 = backup_lib.stage_config_repo(
        sources=[spec],
        stage_dir=stage2,
        existing_repo_dir=repo_dir,
        key_file=key_file,
        max_file_bytes=1024 * 1024,
        encrypt_fn=fake_encrypt,
    )
    assert s2.encrypted_reused == 0
    assert s2.encrypted_new == 1
    assert fake_encrypt.call_count == 1
    assert backup_lib.scan_plaintext_tree(stage2) == []


def test_config_git_backup_pushes_unpushed_commits_when_clean_and_trap_on_error(
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    bare_remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)

    repo_dir = tmp_path / "cfg-repo"
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    _git(repo_dir, "config", "user.name", "Dalton Test")
    _git(repo_dir, "config", "user.email", "dalton@example.com")
    _git(repo_dir, "remote", "add", "origin", str(bare_remote))

    key_file = home_dir / ".config" / "dalton" / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "alerts.log"
    log_file = home_dir / "Library" / "Logs" / "backup.log"

    prod_checkout = tmp_path / "prod"
    prod_var = prod_checkout / "var" / "dalton"
    user_cfg = home_dir / ".config" / "dalton"
    launchagents = home_dir / "Library" / "LaunchAgents"
    backup_scripts = home_dir / "Backups" / "dalton"
    colima = home_dir / ".colima"
    for d in (prod_var, user_cfg, launchagents, backup_scripts, colima):
        d.mkdir(parents=True)
    (prod_var / "cfg.yaml").write_text("mode: prod\n", encoding="utf-8")

    env_file = tmp_path / "offsite-backup.env"
    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=repo_dir,
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=prod_checkout,
        prod_var=prod_var,
        user_config=user_cfg,
        launchagents=launchagents,
        backup_scripts=backup_scripts,
        colima=colima,
        ext_mount=tmp_path / "unmounted",
    )

    sub_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
        "DALTON_REPO_PYTHON": sys.executable,
    }

    # Initial run commits & pushes.
    res1 = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_config_git_backup.sh")],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode == 0, res1.stderr

    # Add a local unpushed commit while leaving tracked backup sources unchanged.
    _git(repo_dir, "commit", "--allow-empty", "-m", "unpushed local commit from prior outage")
    ahead_before = _git(repo_dir, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
    assert ahead_before == "1"

    # Next scheduled run has 0 file changes, yet MUST push the pending commit!
    res2 = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_config_git_backup.sh")],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode == 0, res2.stderr
    ahead_after = _git(repo_dir, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
    assert ahead_after == "0"
    assert "unpushed local commit" in _git(bare_remote, "log", "-n", "1", "--format=%s").stdout

    # Verify unified EXIT/ERR trap preserves exit code, calls gateway BEFORE log_msg, and tolerates unwritable log_file.
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    curl_hit = tmp_path / "curl_hit.txt"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        f"#!/usr/bin/env python3\nimport pathlib, sys\npathlib.Path({str(curl_hit)!r}).write_text(sys.stdin.read())\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    unwritable_log_dir = tmp_path / "log_is_a_dir"
    unwritable_log_dir.mkdir()
    trap_script = tmp_path / "trap_probe.sh"
    trap_script.write_text(
        f"""#!/usr/bin/env bash
SCRIPT_NAME="trap_probe"
source "{OPS_DIR / '_common.sh'}"
DALTON_OFFSITE_BACKUP_LOG_FILE="{unwritable_log_dir}"
DALTON_BACKUP_ALERTS_FILE="{alerts_file}"
DALTON_CHANNEL_GATEWAY_URL="http://127.0.0.1:8962"
INTERNAL_SERVICE_TOKEN="tok_trap_test"
DALTON_REPO_PYTHON="{sys.executable}"
install_failure_trap
(exit 19)
""",
        encoding="utf-8",
    )
    res_trap = subprocess.run(
        ["bash", str(trap_script)],
        env={"PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "HOME": str(home_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_trap.returncode == 19
    assert curl_hit.is_file()
    assert "UNEXPECTED_FAILURE" in alerts_file.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "darwin", reason="needs bsdtar: -s rewrite and ^ anchoring")
def test_weekly_streaming_upload_partial_verify_docker_ps_fail_and_redis_stdin(
    tmp_path: Path,
) -> None:
    import tarfile

    home_dir = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    drive_store = tmp_path / "drive_store"
    ext_mount = tmp_path / "ext_ssd"
    prod_dir = tmp_path / "prod"
    for d in (home_dir, fake_bin, drive_store, ext_mount, prod_dir):
        d.mkdir(parents=True)

    # Create sample .tar streams on the fly in fake docker.
    minio_tar = tmp_path / "minio_src.tar.gz"
    minio_inner = tmp_path / "minio_blob.bin"
    minio_inner.write_bytes(b"MINIO-DATA-12345")
    with tarfile.open(minio_tar, "w:gz") as tf:
        tf.add(minio_inner, arcname="bucket/obj.txt")

    sb_small_tar = tmp_path / "sb_small.tar"
    sb_inner = tmp_path / "ws.txt"
    sb_inner.write_bytes(b"SANDBOX-SMALL-OK")
    with tarfile.open(sb_small_tar, "w") as tf:
        tf.add(sb_inner, arcname="workspace/ws.txt")

    sb_huge_tar = tmp_path / "sb_huge.tar"
    with sb_huge_tar.open("wb") as fh:
        fh.write(b"HUGE" * (1024 * 600))  # ~2.4 MB > test 1 MB override or test 1B

    prod_env = prod_dir / ".env"
    prod_env.write_text("REDIS_PASSWORD=redis-stdin-secret-pw\n", encoding="utf-8")

    redis_exec_record = tmp_path / "redis_exec_record.json"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env python3
import json, os, pathlib, sys

args = sys.argv[1:]
cmd = args[0] if args else ""
if cmd == "info":
    sys.exit(0)
elif cmd == "volume":
    sys.exit(0)
elif cmd == "ps":
    if os.environ.get("FAKE_DOCKER_PS_FAIL") == "1":
        sys.stderr.write("Cannot connect to Docker daemon\\n")
        sys.exit(1)
    if "-a" in args:
        print("langalpha-sandbox-ok")
        print("langalpha-sandbox-noworkspace")
        sys.exit(0)
elif cmd == "run":
    sys.stdout.buffer.write(pathlib.Path({str(minio_tar)!r}).read_bytes())
    sys.exit(0)
elif cmd == "cp":
    target = args[1]
    if target.startswith("langalpha-sandbox-noworkspace:"):
        sys.stderr.write("Error response from daemon: Could not find the file /home/workspace\\n")
        sys.exit(1)
    sys.stdout.buffer.write(pathlib.Path({str(sb_small_tar)!r}).read_bytes())
    sys.exit(0)
elif cmd == "inspect":
    sys.exit(0)
elif cmd == "exec":
    stdin_data = sys.stdin.read()
    pathlib.Path({str(redis_exec_record)!r}).write_text(
        json.dumps({{"argv": args, "stdin": stdin_data}})
    )
    sys.stdout.buffer.write(b"REDIS0011synthetic-rdb-payload")
    sys.exit(0)
sys.exit(1)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    about_file = tmp_path / "about.json"
    about_file.write_text('{"free": 100000000}\n', encoding="utf-8")
    proxy_check_file = tmp_path / "proxy_seen.txt"
    rcat_sizes_log = tmp_path / "rcat_sizes.jsonl"
    _write_fake_rclone(
        fake_bin,
        drive_store=drive_store,
        about_file=about_file,
        proxy_check_file=proxy_check_file,
        rcat_sizes_log=rcat_sizes_log,
    )

    prod_var = prod_dir / "var"
    for rel_keep in (
        ".dalton-freeze/snapshot.bin",
        "openclaw-data/skills/x/data/dataset.json",
        "openclaw-data/skills/x/state/state.json",
        "openclaw-data/skills/x/output/report.md",
        "openclaw-data/skills/x/events/events.jsonl",
        "asr_jobs/job1/transcript.txt",
        "reextract/item.json",
        "nested/openclaw-data/temp/a",
        "nested/openclaw-data/skills/x/temp/a",
        "ai-finance/fake.sqlite",
    ):
        p = prod_var / rel_keep
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"keep:{rel_keep}\n", encoding="utf-8")
    for rel_drop in (
        "openclaw-data/skills/x/venv/lib/site.py",
        ".venv/pyvenv.cfg",
        "__pycache__/mod.cpython-313.pyc",
        "loose.pyc",
        "node_modules/pkg/index.js",
        ".DS_Store",
        "pr_baseline/report.json",
        "tmp/scratch.txt",
        "openclaw-data/temp/drop.txt",
        "openclaw-data/skills/x/temp/drop.tmp",
        "run-secrets/secret.txt",
        "openclaw-data/skills/x/run-secrets/k.txt",
        ".dalton-outbox-123/msg.json",
    ):
        p = prod_var / rel_drop
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"drop:{rel_drop}\n", encoding="utf-8")

    # Live WAL-mode SQLite databases (including .db extension) with uncheckpointed writes.
    sig_db = prod_var / "ai-finance" / "ai_signal_memory.sqlite"
    sig_db.parent.mkdir(parents=True, exist_ok=True)
    sig_conn = sqlite3.connect(sig_db)
    sig_conn.execute("PRAGMA journal_mode=WAL")
    sig_conn.execute("PRAGMA wal_autocheckpoint=0")
    sig_conn.execute("CREATE TABLE signals(id INT, sym TEXT)")
    sig_conn.execute("INSERT INTO signals VALUES (1, 'NVDA'), (2, 'MSFT')")
    sig_conn.commit()

    vec_db = prod_var / "openclaw-data" / "skills" / "market-digest" / "vectors.db"
    vec_db.parent.mkdir(parents=True, exist_ok=True)
    vec_conn = sqlite3.connect(vec_db)
    vec_conn.execute("PRAGMA journal_mode=WAL")
    vec_conn.execute("PRAGMA wal_autocheckpoint=0")
    vec_conn.execute("CREATE TABLE vec(id INT, tag TEXT)")
    vec_conn.execute("INSERT INTO vec VALUES (10, 'wal-live')")
    vec_conn.commit()

    key_file = home_dir / ".config" / "dalton" / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "alerts.log"
    log_file = home_dir / "Library" / "Logs" / "backup.log"
    env_file = tmp_path / "offsite-backup.env"
    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=tmp_path / "repo",
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=prod_dir,
        prod_var=prod_var,
        user_config=home_dir / ".config" / "dalton",
        launchagents=home_dir / "Library" / "LaunchAgents",
        backup_scripts=tmp_path / "bs",
        colima=home_dir / ".colima",
        ext_mount=ext_mount,
    )
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f'DALTON_PROD_ENV_FILE="{prod_env}"\n')

    mounted_py = _make_mounted_python_wrapper(tmp_path, ext_mount)
    sub_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home_dir),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
        "DALTON_REPO_PYTHON": str(mounted_py),
        "http_proxy": "http://127.0.0.1:7890",
    }

    # Case 1: docker ps -a failure must abort with DOCKER_LIST_FAILED instead of skipping sandboxes silently.
    res_ps_fail = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
        env={**sub_env, "FAKE_DOCKER_PS_FAIL": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_ps_fail.returncode != 0
    assert "DOCKER_LIST_FAILED" in alerts_file.read_text(encoding="utf-8")

    # Case 2: Hash mismatch on Drive deletes partial and fails.
    res_hash_fail = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
        env={**sub_env, "FAKE_RCLONE_BAD_HASH": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_hash_fail.returncode != 0
    assert "RCLONE_HASH_MISMATCH" in alerts_file.read_text(encoding="utf-8")
    today_str = dt.datetime.now().strftime("%Y-%m-%d")
    week_drive = drive_store / "DaltonBackup" / "weekly" / today_str
    assert list(week_drive.glob("*.partial-*")) == []

    # Case 3: Happy path streams MinIO + Sandbox + Redis directly, Redis password only via stdin, 0 local archives.
    try:
        res_ok = subprocess.run(
            ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
            env=sub_env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        sig_conn.close()
        vec_conn.close()
    assert res_ok.returncode == 0, res_ok.stderr
    assert not proxy_check_file.exists()
    assert not (ext_mount / "Dalton" / "backups" / "weekly" / today_str).exists()
    assert list((ext_mount / "Dalton" / "backups" / ".tmp").glob("*")) == []
    assert list(week_drive.glob("*.partial-*")) == []

    assert (week_drive / f"minio-{today_str}.tar.gz.enc").is_file()
    assert (week_drive / f"sandbox-langalpha-sandbox-ok-{today_str}.tar.gz.enc").is_file()
    assert not (week_drive / f"sandbox-langalpha-sandbox-noworkspace-{today_str}.tar.gz.enc").exists()
    redis_enc = week_drive / f"redis-{today_str}.rdb.enc"
    assert redis_enc.is_file()
    var_enc = week_drive / f"var-dalton-{today_str}.tar.gz.enc"
    assert var_enc.is_file()

    # Verify Redis password passed via stdin and NEVER exposed in docker exec argv.
    exec_info = json.loads(redis_exec_record.read_text(encoding="utf-8"))
    assert all("redis-stdin-secret-pw" not in a for a in exec_info["argv"])
    assert exec_info["stdin"] == "redis-stdin-secret-pw\n"

    # Decrypt streamed Redis RDB to prove end-to-end byte integrity.
    dec = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-in",
            str(redis_enc),
            "-pass",
            f"file:{key_file}",
        ],
        capture_output=True,
        check=True,
    )
    assert dec.stdout == b"REDIS0011synthetic-rdb-payload"

    # Decrypt streamed var-dalton tar.gz and verify included/excluded archive members and extracted SQLite integrity.
    var_tar_gz = _openssl_decrypt(var_enc, key_file)
    with tarfile.open(fileobj=io.BytesIO(var_tar_gz), mode="r:gz") as tf:
        var_members = {m.name.removeprefix("./") for m in tf.getmembers() if m.isfile()}
    assert {
        ".dalton-freeze/snapshot.bin",
        "openclaw-data/skills/x/data/dataset.json",
        "openclaw-data/skills/x/state/state.json",
        "openclaw-data/skills/x/output/report.md",
        "openclaw-data/skills/x/events/events.jsonl",
        "asr_jobs/job1/transcript.txt",
        "reextract/item.json",
        "nested/openclaw-data/temp/a",
        "nested/openclaw-data/skills/x/temp/a",
        "ai-finance/fake.sqlite",
        "ai-finance/ai_signal_memory.sqlite",
        "openclaw-data/skills/market-digest/vectors.db",
    }.issubset(var_members)
    for excluded_rel in (
        "openclaw-data/skills/x/venv/lib/site.py",
        ".venv/pyvenv.cfg",
        "__pycache__/mod.cpython-313.pyc",
        "loose.pyc",
        "node_modules/pkg/index.js",
        ".DS_Store",
        "pr_baseline/report.json",
        "tmp/scratch.txt",
        "openclaw-data/temp/drop.txt",
        "openclaw-data/skills/x/temp/drop.tmp",
        "run-secrets/secret.txt",
        "openclaw-data/skills/x/run-secrets/k.txt",
        ".dalton-outbox-123/msg.json",
        "ai-finance/ai_signal_memory.sqlite-wal",
        "ai-finance/ai_signal_memory.sqlite-shm",
        "ai-finance/ai_signal_memory.sqlite-journal",
        "openclaw-data/skills/market-digest/vectors.db-wal",
        "openclaw-data/skills/market-digest/vectors.db-shm",
        "openclaw-data/skills/market-digest/vectors.db-journal",
    ):
        assert excluded_rel not in var_members

    # Extract with real tar and verify restored SQLite databases at original paths pass integrity_check.
    extract_dir = tmp_path / "var_extracted"
    extract_dir.mkdir()
    subprocess.run(
        ["tar", "xzf", "-", "-C", str(extract_dir)],
        input=var_tar_gz,
        capture_output=True,
        check=True,
    )
    restored_sig = sqlite3.connect(extract_dir / "ai-finance" / "ai_signal_memory.sqlite")
    try:
        assert restored_sig.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert restored_sig.execute("SELECT id, sym FROM signals ORDER BY id").fetchall() == [
            (1, "NVDA"),
            (2, "MSFT"),
        ]
    finally:
        restored_sig.close()
    restored_vec = sqlite3.connect(
        extract_dir / "openclaw-data" / "skills" / "market-digest" / "vectors.db"
    )
    try:
        assert restored_vec.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert restored_vec.execute("SELECT id, tag FROM vec").fetchall() == [(10, "wal-live")]
    finally:
        restored_vec.close()

    # Case 4: Missing DALTON_PROD_VAR_DIR emits VAR_DALTON_BACKUP_FAILED alert.
    missing_env_file = tmp_path / "offsite-backup-missing-var.env"
    missing_env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + f'\nDALTON_PROD_VAR_DIR="{tmp_path / "does-not-exist-var"}"\n',
        encoding="utf-8",
    )
    missing_var_env = {**sub_env, "DALTON_OFFSITE_BACKUP_ENV_FILE": str(missing_env_file)}
    res_missing_var = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
        env=missing_var_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_missing_var.returncode != 0
    assert "VAR_DALTON_BACKUP_FAILED" in alerts_file.read_text(encoding="utf-8")



def _openssl_decrypt(enc_path: Path, key_file: Path) -> bytes:
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-in",
            str(enc_path),
            "-pass",
            f"file:{key_file}",
        ],
        capture_output=True,
        check=True,
    )
    return proc.stdout


def test_default_encrypt_allowlist_codex_r2_four_cases_and_fallback_encrypt(
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    bare_remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare_remote)],
        check=True,
        capture_output=True,
        text=True,
    )

    repo_dir = tmp_path / "dalton-config-repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo_dir, "config", "user.name", "Dalton Test")
    _git(repo_dir, "config", "user.email", "dalton-test@example.com")
    _git(repo_dir, "remote", "add", "origin", str(bare_remote))

    prod_checkout = tmp_path / "prod_checkout"
    prod_var = prod_checkout / "var" / "dalton"
    prod_var.mkdir(parents=True)
    (prod_checkout / ".env").write_text("API_KEY=secret-1\n", encoding="utf-8")

    # Allowlisted plaintext under var/dalton (*.md, *.lock) vs non-allowlisted plain text.
    (prod_var / "README.md").write_text("# Dalton Docs\n", encoding="utf-8")
    (prod_var / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (prod_var / "plain_notes.txt").write_text("not in allowlist\n", encoding="utf-8")
    # Codex R2 missed cases in non-allowlisted files + allowlisted fallback files.
    missed_json_num = b'{\n  "password":\n  987654\n}\n'
    missed_json_str = b'{\n  "password":\n  "actual-secret"\n}\n'
    (prod_var / "missed_num.json").write_bytes(missed_json_num)
    (prod_var / "missed_str.json").write_bytes(missed_json_str)
    missed_yaml_md = b"---\nauth:\n  password:\n    'actual-secret'\n"
    (prod_var / "leaked_yaml.md").write_bytes(missed_yaml_md)

    user_cfg = home_dir / ".config" / "dalton"
    user_cfg.mkdir(parents=True)
    missed_yaml_cfg = b'auth:\n  password:\n    "actual-secret"\n'
    (user_cfg / "missed_yaml.yaml").write_bytes(missed_yaml_cfg)

    launchagents = home_dir / "Library" / "LaunchAgents"
    launchagents.mkdir(parents=True)
    (launchagents / "com.dalton.backup.plist").write_text(
        "<plist><dict/></plist>\n", encoding="utf-8"
    )
    secret_plist = b"<plist><dict><key>DB_PASSWORD</key><string>hunter2</string></dict></plist>\n"
    (launchagents / "com.dalton.secret.plist").write_bytes(secret_plist)
    (launchagents / "other.plist").write_text(
        "<plist><dict/></plist>\n", encoding="utf-8"
    )

    backup_scripts = home_dir / "Backups" / "dalton"
    backup_scripts.mkdir(parents=True)
    (backup_scripts / "run.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    (backup_scripts / "helper.py").write_text("print('ok')\n", encoding="utf-8")
    leaked_sh = b'#!/bin/bash\nPASSWORD="#actual-secret"\n'
    leaked_py = b'cfg = {\n  "password":\n  987654\n}\n'
    (backup_scripts / "leaked_hash.sh").write_bytes(leaked_sh)
    (backup_scripts / "leaked_multiline.py").write_bytes(leaked_py)

    colima = home_dir / ".colima"
    colima.mkdir(parents=True)
    (colima / "colima.yaml").write_text("cpu: 4\n", encoding="utf-8")

    key_file = user_cfg / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    log_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup.log"
    env_file = tmp_path / "offsite-backup.env"

    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=repo_dir,
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=prod_checkout,
        prod_var=prod_var,
        user_config=user_cfg,
        launchagents=launchagents,
        backup_scripts=backup_scripts,
        colima=colima,
    )

    res = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_config_git_backup.sh")],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home_dir),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
            "DALTON_REPO_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    # Clean allowlisted files remain plaintext; non-allowlisted & fallback-detected files are .enc.
    for rel_plain in (
        "launch_agents/com.dalton.backup.plist",
        "backup_scripts/run.sh",
        "backup_scripts/helper.py",
    ):
        assert (repo_dir / rel_plain).is_file(), f"missing plaintext {rel_plain}"
        assert not (repo_dir / f"{rel_plain}.enc").exists()

    assert not (repo_dir / "launch_agents" / "other.plist").exists()
    assert not backup_lib.is_plaintext_allowlisted("launch_agents", "other.plist")

    expected_encrypted: dict[str, bytes] = {
        "var_dalton/plain_notes.txt": b"not in allowlist\n",
        "var_dalton/README.md": None,
        "var_dalton/uv.lock": None,
        "var_dalton/missed_num.json": missed_json_num,
        "var_dalton/missed_str.json": missed_json_str,
        "var_dalton/leaked_yaml.md": missed_yaml_md,
        "dot_config_dalton/missed_yaml.yaml": missed_yaml_cfg,
        "launch_agents/com.dalton.secret.plist": secret_plist,
        "backup_scripts/leaked_hash.sh": leaked_sh,
        "backup_scripts/leaked_multiline.py": leaked_py,
        "colima/colima.yaml": b"cpu: 4\n",
        "prod_overrides/.env": b"API_KEY=secret-1\n",
    }
    for rel_plain, raw_bytes in expected_encrypted.items():
        assert not (repo_dir / rel_plain).exists(), f"unexpected plaintext {rel_plain}"
        enc_path = repo_dir / f"{rel_plain}.enc"
        assert enc_path.is_file(), f"missing ciphertext {rel_plain}.enc"
        if raw_bytes is not None:
            assert _openssl_decrypt(enc_path, key_file) == raw_bytes


def test_key_rotation_reencrypts_all_files_on_replace_and_delete_rebuild(
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    bare_remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare_remote)],
        check=True,
        capture_output=True,
        text=True,
    )

    repo_dir = tmp_path / "dalton-config-repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo_dir, "config", "user.name", "Dalton Test")
    _git(repo_dir, "config", "user.email", "dalton-test@example.com")
    _git(repo_dir, "remote", "add", "origin", str(bare_remote))

    prod_checkout = tmp_path / "prod_checkout"
    prod_var = prod_checkout / "var" / "dalton"
    prod_var.mkdir(parents=True)
    (prod_var / "state.json").write_text('{"seq": 1}\n', encoding="utf-8")
    (prod_checkout / ".env").write_text("POSTGRES_PASSWORD=pgsecret\n", encoding="utf-8")

    user_cfg = home_dir / ".config" / "dalton"
    user_cfg.mkdir(parents=True)
    (user_cfg / "settings.json").write_text('{"mode": "prod"}\n', encoding="utf-8")

    launchagents = home_dir / "Library" / "LaunchAgents"
    launchagents.mkdir(parents=True)
    backup_scripts = home_dir / "Backups" / "dalton"
    backup_scripts.mkdir(parents=True)
    (backup_scripts / "clean.sh").write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    colima = home_dir / ".colima"
    colima.mkdir(parents=True)

    key_file = user_cfg / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    log_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup.log"
    env_file = tmp_path / "offsite-backup.env"

    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=repo_dir,
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=prod_checkout,
        prod_var=prod_var,
        user_config=user_cfg,
        launchagents=launchagents,
        backup_scripts=backup_scripts,
        colima=colima,
    )

    sub_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
        "DALTON_REPO_PYTHON": sys.executable,
    }
    script = OPS_DIR / "dalton_config_git_backup.sh"

    # Run 1: generate initial key, encrypt 3 files, record key_fingerprint.
    r1 = subprocess.run(["bash", str(script)], env=sub_env, capture_output=True, text=True, check=False)
    assert r1.returncode == 0, r1.stderr
    fp1 = backup_lib.key_fingerprint(key_file)
    manifest1 = json.loads((repo_dir / backup_lib.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest1["key_fingerprint"] == fp1
    old_key_copy = tmp_path / "old_key_1"
    old_key_copy.write_bytes(key_file.read_bytes())

    # Run 2: replace backup.key with a new passphrase -> all 3 .enc files must re-encrypt.
    key_file.write_text("rotated-new-passphrase-2026\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    fp2 = backup_lib.key_fingerprint(key_file)
    assert fp2 != fp1

    r2 = subprocess.run(["bash", str(script)], env=sub_env, capture_output=True, text=True, check=False)
    assert r2.returncode == 0, r2.stderr
    manifest2 = json.loads((repo_dir / backup_lib.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest2["key_fingerprint"] == fp2
    log_text = log_file.read_text(encoding="utf-8")
    stage_lines = [line for line in log_text.splitlines() if "encrypted_reused" in line]
    assert stage_lines and '"encrypted_reused": 0' in stage_lines[-1]
    assert '"encrypted_new": 3' in stage_lines[-1]
    assert _openssl_decrypt(repo_dir / "prod_overrides" / ".env.enc", key_file) == b"POSTGRES_PASSWORD=pgsecret\n"
    assert _openssl_decrypt(repo_dir / "var_dalton" / "state.json.enc", key_file) == b'{"seq": 1}\n'
    with pytest.raises(subprocess.CalledProcessError):
        _openssl_decrypt(repo_dir / "prod_overrides" / ".env.enc", old_key_copy)

    # Run 3: delete backup.key -> auto-rebuilds 0600 key and re-encrypts all 3 .enc files.
    key2_copy = tmp_path / "old_key_2"
    key2_copy.write_bytes(key_file.read_bytes())
    key_file.unlink()

    r3 = subprocess.run(["bash", str(script)], env=sub_env, capture_output=True, text=True, check=False)
    assert r3.returncode == 0, r3.stderr
    assert key_file.is_file()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    fp3 = backup_lib.key_fingerprint(key_file)
    assert fp3 != fp2
    manifest3 = json.loads((repo_dir / backup_lib.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest3["key_fingerprint"] == fp3
    assert _openssl_decrypt(repo_dir / "prod_overrides" / ".env.enc", key_file) == b"POSTGRES_PASSWORD=pgsecret\n"
    assert _openssl_decrypt(repo_dir / "dot_config_dalton" / "settings.json.enc", key_file) == b'{"mode": "prod"}\n'
    with pytest.raises(subprocess.CalledProcessError):
        _openssl_decrypt(repo_dir / "dot_config_dalton" / "settings.json.enc", key2_copy)


@pytest.mark.parametrize(
    "script_name",
    [
        "dalton_config_git_backup.sh",
        "dalton_data_backup.sh",
        "dalton_db_offsite.sh",
    ],
)
def test_early_failure_trap_on_unreadable_or_syntax_error_env_file(
    tmp_path: Path, script_name: str
) -> None:
    script = OPS_DIR / script_name

    # Case 1: syntax error in offsite-backup.env -> default alerts log still receives ERROR alert.
    home1 = tmp_path / f"home_syntax_{script_name}"
    home1.mkdir()
    bad_env = tmp_path / f"bad_syntax_{script_name}.env"
    bad_env.write_text('DALTON_BROKEN="unterminated quote\n', encoding="utf-8")

    res_syntax = subprocess.run(
        ["bash", str(script)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home1),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(bad_env),
            "DALTON_REPO_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_syntax.returncode != 0
    assert "unbound variable" not in res_syntax.stderr
    alerts1 = home1 / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    assert alerts1.is_file()
    alerts1_text = alerts1.read_text(encoding="utf-8")
    assert "[ERROR]" in alerts1_text
    assert "ENV_SYNTAX_ERROR" in alerts1_text

    # Case 2: symlink env file -> rejected before sourcing, writes INVALID_ENV_FILE alert.
    home2 = tmp_path / f"home_symlink_{script_name}"
    home2.mkdir()
    real_env = tmp_path / f"real_{script_name}.env"
    real_env.write_text("DALTON_FOO=bar\n", encoding="utf-8")
    sym_env = tmp_path / f"sym_{script_name}.env"
    sym_env.symlink_to(real_env)

    res_sym = subprocess.run(
        ["bash", str(script)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home2),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(sym_env),
            "DALTON_REPO_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_sym.returncode != 0
    assert "unbound variable" not in res_sym.stderr
    alerts2 = home2 / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    assert alerts2.is_file()
    alerts2_text = alerts2.read_text(encoding="utf-8")
    assert "[ERROR]" in alerts2_text
    assert "INVALID_ENV_FILE" in alerts2_text

    # Case 3: unreadable env file (chmod 0000 for non-root user) -> writes ENV_LOAD_FAILED alert.
    if os.geteuid() != 0:
        home3 = tmp_path / f"home_unreadable_{script_name}"
        home3.mkdir()
        unreadable_env = tmp_path / f"unreadable_{script_name}.env"
        unreadable_env.write_text("DALTON_FOO=bar\n", encoding="utf-8")
        os.chmod(unreadable_env, 0o000)
        try:
            res_unreadable = subprocess.run(
                ["bash", str(script)],
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(home3),
                    "DALTON_OFFSITE_BACKUP_ENV_FILE": str(unreadable_env),
                    "DALTON_REPO_PYTHON": sys.executable,
                },
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            os.chmod(unreadable_env, 0o600)

        assert res_unreadable.returncode != 0
        assert "unbound variable" not in res_unreadable.stderr
        alerts3 = home3 / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
        assert alerts3.is_file()
        alerts3_text = alerts3.read_text(encoding="utf-8")
        assert "[ERROR]" in alerts3_text
        assert "ENV_LOAD_FAILED" in alerts3_text


def test_existing_repo_root_files_do_not_block_commits(tmp_path):
    """README.md / .gitignore at the backup repo root stay plaintext and are still scanned."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("Dalton config backup. Secrets are encrypted.\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    assert backup_lib.scan_plaintext_tree(repo) == []
    (repo / "README.md").write_text("API_KEY=sk-live-abcdef0123456789\n", encoding="utf-8")
    findings = backup_lib.scan_plaintext_tree(repo)
    assert [rel for rel, _ in findings] == ["README.md"]


def test_var_dalton_pure_helpers_escaping_and_retry_logic(tmp_path: Path) -> None:
    """Pure-Python tar-cmd escaping, exclude filtering, and pipe-status retry checks."""
    # F1: SQLite paths containing glob metacharacters must be backslash-escaped in --exclude.
    cmd = backup_lib.build_var_dalton_tar_cmd(
        tmp_path / "src",
        excludes="tmp",
        sqlite_paths=[r"state/cache[1].db", r"weird\a*b?.sqlite"],
        staging_dir=tmp_path / "staging",
    )
    assert r"^./state/cache\[1\].db" in cmd
    assert r"^./state/cache\[1\].db-wal" in cmd
    assert r"^./state/cache\[1\].db-shm" in cmd
    assert r"^./state/cache\[1\].db-journal" in cmd
    assert r"^./weird\\a\*b\?.sqlite" in cmd
    assert r"^./weird\\a\*b\?.sqlite-wal" in cmd
    cmd = backup_lib.build_var_dalton_tar_cmd(
        tmp_path / "src",
        excludes="tmp",
        sqlite_paths=["state/cash$", "state/a^b"],
        staging_dir=tmp_path / "staging",
    )
    assert r"^./state/cash\$" in cmd
    assert r"^./state/cash\$-wal" in cmd
    assert r"^./state/a\^b" in cmd

    # F2: Retry only when tar (stage 0) failed and all downstream stages exited 0.
    assert backup_lib.should_retry_tar_failure(1, "1 0 0 0") is True
    # Only the full four-stage pipe status may trigger a retry.
    assert backup_lib.should_retry_tar_failure(1, "1 0") is False
    assert backup_lib.should_retry_tar_failure(1, "1 0 0") is False
    assert backup_lib.should_retry_tar_failure(1, "1 0 0 0 0") is False
    assert backup_lib.should_retry_tar_failure(1, "141 1 1 7") is False
    assert backup_lib.should_retry_tar_failure(1, "0 0 0 1") is False
    assert backup_lib.should_retry_tar_failure(2, "1 0 0 0") is False

    for attempt, pipe_str, expected_rc in (
        ("1", "1 0 0 0", 0),
        ("1", "141 1 1 7", 1),
        ("1", "0 0 0 1", 1),
        ("2", "1 0 0 0", 1),
    ):
        proc = subprocess.run(
            [
                sys.executable,
                str(OPS_DIR / "backup_lib.py"),
                "should-retry-tar",
                "--attempt",
                attempt,
                "--pipe-status",
                pipe_str,
                "--max-retries",
                "1",
            ],
            capture_output=True,
            check=False,
        )
        assert proc.returncode == expected_rc

    summary = backup_lib.summarize_tar_stderr(
        "tar: ./vanished.tmp: Cannot stat: No such file or directory\ntar: Error exit delayed\n"
    )
    assert "\n" not in summary
    assert "./vanished.tmp: Cannot stat: No such file or directory" in summary
    assert len(backup_lib.summarize_tar_stderr("x" * 500, max_chars=50)) == 50


@pytest.mark.skipif(sys.platform != "darwin", reason="needs bsdtar: -s rewrite and ^ anchoring")
def test_var_dalton_excludes_and_tar_cmd(tmp_path: Path) -> None:
    """var-dalton weekly excludes anchor slash rules to root while skipping nested non-slash names."""
    import tarfile

    var_dir = tmp_path / "var" / "dalton"
    expected_excluded = {
        "skills/x/venv/lib/python.py",
        "venv/bin/activate",
        ".venv/pyvenv.cfg",
        "__pycache__/mod.cpython-313.pyc",
        "skills/x/__pycache__/helper.cpython-313.pyc",
        "root_compiled.pyc",
        "node_modules/pkg/index.js",
        ".DS_Store",
        "skills/x/.DS_Store",
        "pr_baseline/summary.json",
        "tmp/scratch.txt",
        "openclaw-data/temp/a",
        "openclaw-data/skills/x/temp/scratch.tmp",
        "run-secrets/token.json",
        "openclaw-data/skills/x/run-secrets/api.key",
        ".dalton-outbox-99/pending.json",
    }
    expected_included = {
        "skills/x/data/metrics.json",
        "openclaw-data/skills/x/data/table.parquet",
        "openclaw-data/skills/x/state/cursor.json",
        "openclaw-data/skills/x/output/report.md",
        "openclaw-data/skills/x/events/stream.jsonl",
        "openclaw-data/skills/x/temp-keep/a",
        "nested/openclaw-data/temp/a",
        "nested/openclaw-data/skills/x/temp/a",
        ".dalton-freeze/2026-11-14.tar",
        "asr_jobs/job-001/transcript.txt",
        "reextract/doc-123.json",
    }
    for rel in expected_excluded | expected_included:
        fpath = var_dir / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(f"payload:{rel}\n", encoding="utf-8")

    included, excluded = backup_lib.filter_var_dalton_paths(var_dir)
    assert set(included) == expected_included
    assert set(excluded) == expected_excluded

    # Slash rules emit `^./` prefix to anchor at archive root; non-slash rules match at any depth.
    exclude_args = backup_lib.build_tar_exclude_args()
    expected_exclude_args = [
        arg
        for pat in backup_lib.DEFAULT_DATA_VAR_EXCLUDES
        for arg in ("--exclude", f"^./{pat}" if "/" in pat else pat)
    ]
    assert exclude_args == expected_exclude_args
    tar_cmd = backup_lib.build_var_dalton_tar_cmd(var_dir)
    assert tar_cmd == ["tar", "czf", "-", *exclude_args, "-C", str(var_dir), "."]

    # Exercise create_autospec runner double with constructed tar cmd.
    captured_cmds: list[list[str]] = []

    def _run_stub(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured_cmds.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"TAR", stderr=b"")

    fake_run = create_autospec(subprocess.run, side_effect=_run_stub)
    fake_run(tar_cmd, capture_output=True, check=True)
    assert captured_cmds == [tar_cmd]

    # Run real tar with constructed args to verify bsdtar archive matches pure filter
    # (specifically keeping nested/openclaw-data/temp/a while excluding openclaw-data/temp/a).
    tar_proc = subprocess.run(
        tar_cmd,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
        capture_output=True,
        check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(tar_proc.stdout), mode="r:gz") as tf:
        archived_files = {
            m.name.removeprefix("./") for m in tf.getmembers() if m.isfile()
        }
    assert archived_files == expected_included
    assert "nested/openclaw-data/temp/a" in archived_files
    assert "openclaw-data/temp/a" not in archived_files
    assert archived_files.isdisjoint(expected_excluded)


@pytest.mark.skipif(sys.platform != "darwin", reason="needs bsdtar: -s rewrite and ^ anchoring")
def test_snapshot_sqlite_wal_header_detection_excludes_and_failure(
    tmp_path: Path,
) -> None:
    src_dir = tmp_path / "var_dalton"
    staging_dir = tmp_path / "sqlite-staging"
    src_dir.mkdir()

    # 1. WAL-mode DB (.sqlite) with committed uncheckpointed writes + uncommitted write.
    wal_db = src_dir / "ai-finance" / "ai_signal_memory.sqlite"
    wal_db.parent.mkdir(parents=True)
    c_wal = sqlite3.connect(wal_db)
    c_wal.execute("PRAGMA journal_mode=WAL")
    c_wal.execute("PRAGMA wal_autocheckpoint=0")
    c_wal.execute("CREATE TABLE items(id INT, val TEXT)")
    c_wal.execute("INSERT INTO items VALUES (1, 'base')")
    c_wal.commit()
    c_wal.execute("INSERT INTO items VALUES (2, 'uncheckpointed-wal')")
    c_wal.commit()
    c_wal.execute("BEGIN IMMEDIATE")
    c_wal.execute("INSERT INTO items VALUES (3, 'uncommitted')")

    # 2. .db extension and extensionless SQLite files must be detected by 16-byte header.
    db_ext = src_dir / "openclaw-data" / "skills" / "market-digest" / "vectors.db"
    db_ext.parent.mkdir(parents=True)
    c_vec = sqlite3.connect(db_ext)
    c_vec.execute("CREATE TABLE v(k TEXT)")
    c_vec.execute("INSERT INTO v VALUES ('ok-db')")
    c_vec.commit()
    c_vec.close()

    noext_db = src_dir / "custom_store"
    c_noext = sqlite3.connect(noext_db)
    c_noext.execute("CREATE TABLE s(v INT)")
    c_noext.execute("INSERT INTO s VALUES (42)")
    c_noext.commit()
    c_noext.close()

    # 3. File named x.sqlite with non-SQLite contents must NOT be treated as SQLite.
    fake_sqlite = src_dir / "x.sqlite"
    fake_sqlite.write_text("not actually a sqlite database", encoding="utf-8")

    # 4. SQLite databases inside excluded directories must NOT be processed.
    for ex_rel in (
        "openclaw-data/temp/skip.db",
        "openclaw-data/skills/s1/temp/skip.sqlite",
        "run-secrets/secret.db",
        ".venv/env.sqlite",
    ):
        ex_path = src_dir / ex_rel
        ex_path.parent.mkdir(parents=True, exist_ok=True)
        c_ex = sqlite3.connect(ex_path)
        c_ex.execute("CREATE TABLE skip(x INT)")
        c_ex.commit()
        c_ex.close()

    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(OPS_DIR / "backup_lib.py"),
                "snapshot-sqlite",
                "--src-dir",
                str(src_dir),
                "--dest-dir",
                str(staging_dir),
                "--excludes",
                ",".join(backup_lib.DEFAULT_DATA_VAR_EXCLUDES),
            ],
            capture_output=True,
            check=True,
        )
    finally:
        c_wal.rollback()
        c_wal.close()

    manifest_paths = [
        item.decode("utf-8") for item in proc.stdout.split(b"\0") if item
    ]
    assert manifest_paths == [
        "ai-finance/ai_signal_memory.sqlite",
        "custom_store",
        "openclaw-data/skills/market-digest/vectors.db",
    ]
    assert not (staging_dir / "x.sqlite").exists()
    assert not (staging_dir / "openclaw-data" / "temp" / "skip.db").exists()
    assert not (staging_dir / "openclaw-data" / "skills" / "s1" / "temp" / "skip.sqlite").exists()
    assert not (staging_dir / "run-secrets" / "secret.db").exists()
    assert not (staging_dir / ".venv" / "env.sqlite").exists()

    snap_wal = sqlite3.connect(staging_dir / "ai-finance" / "ai_signal_memory.sqlite")
    try:
        assert snap_wal.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert snap_wal.execute("SELECT id, val FROM items ORDER BY id").fetchall() == [
            (1, "base"),
            (2, "uncheckpointed-wal"),
        ]
    finally:
        snap_wal.close()

    # Verify build_var_dalton_tar_cmd excludes live DBs + -wal/-shm/-journal and archives staging copies.
    tar_cmd = backup_lib.build_var_dalton_tar_cmd(
        src_dir,
        sqlite_paths=manifest_paths,
        staging_dir=staging_dir,
    )
    for rel in manifest_paths:
        for sfx in ("", "-wal", "-shm", "-journal"):
            assert f"^./{rel}{sfx}" in tar_cmd
    tar_out = subprocess.run(
        tar_cmd,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
        capture_output=True,
        check=True,
    ).stdout
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    subprocess.run(
        ["tar", "xzf", "-", "-C", str(extracted)],
        input=tar_out,
        capture_output=True,
        check=True,
    )
    assert (extracted / "x.sqlite").read_text(encoding="utf-8") == "not actually a sqlite database"
    chk = sqlite3.connect(extracted / "ai-finance" / "ai_signal_memory.sqlite")
    try:
        assert chk.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert chk.execute("SELECT id, val FROM items ORDER BY id").fetchall() == [
            (1, "base"),
            (2, "uncheckpointed-wal"),
        ]
    finally:
        chk.close()

    # Corrupt file with SQLite header must fail snapshot-sqlite with non-zero exit & stderr message.
    corrupt_db = src_dir / "broken.sqlite"
    corrupt_db.write_bytes(backup_lib.SQLITE_FILE_HEADER + b"\xff" * 200)
    proc_fail = subprocess.run(
        [
            sys.executable,
            str(OPS_DIR / "backup_lib.py"),
            "snapshot-sqlite",
            "--src-dir",
            str(src_dir),
            "--dest-dir",
            str(tmp_path / "staging-fail"),
            "--excludes",
            ",".join(backup_lib.DEFAULT_DATA_VAR_EXCLUDES),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_fail.returncode != 0
    assert "broken.sqlite" in proc_fail.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="needs bsdtar: -s rewrite and ^ anchoring")
def test_var_dalton_tar_sqlite_glob_escaping_e2e(tmp_path: Path) -> None:
    """Real bsdtar E2E: SQLite path with `[1]` excludes itself/WAL without excluding `cache1.db`."""
    src_dir = tmp_path / "var_dalton"
    staging_dir = tmp_path / "sqlite-staging"
    state_dir = src_dir / "state"
    state_dir.mkdir(parents=True)

    glob_db = state_dir / "cache[1].db"
    conn = sqlite3.connect(glob_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE cache(k TEXT)")
    conn.execute("INSERT INTO cache VALUES ('snap-cache-1')")
    conn.commit()

    plain_sibling = state_dir / "cache1.db"
    plain_sibling.write_text("plain-cache1-unchanged", encoding="utf-8")

    # A name ending in `$` must not become an end anchor that drops its plain sibling `cash`.
    dconn = sqlite3.connect(state_dir / "cash$")
    dconn.execute("CREATE TABLE t(k TEXT)")
    dconn.execute("INSERT INTO t VALUES ('snap-cash')")
    dconn.commit()
    dconn.close()
    (state_dir / "cash").write_text("plain-cash-unchanged", encoding="utf-8")

    try:
        sqlite_paths = backup_lib.snapshot_sqlite(
            src_dir, staging_dir, excludes=backup_lib.DEFAULT_DATA_VAR_EXCLUDES
        )
    finally:
        conn.close()

    assert sqlite_paths == ["state/cache[1].db", "state/cash$"]
    # Keep a stale -wal next to the source DB to confirm bsdtar excludes it while keeping cache1.db.
    (state_dir / "cache[1].db-wal").write_bytes(b"stale-wal-bytes")

    tar_cmd = backup_lib.build_var_dalton_tar_cmd(
        src_dir,
        excludes=backup_lib.DEFAULT_DATA_VAR_EXCLUDES,
        sqlite_paths=sqlite_paths,
        staging_dir=staging_dir,
    )
    tar_out = subprocess.run(
        tar_cmd,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
        capture_output=True,
        check=True,
    ).stdout

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    subprocess.run(
        ["tar", "xzf", "-", "-C", str(extracted)],
        input=tar_out,
        capture_output=True,
        check=True,
    )
    assert not (extracted / "state" / "cache[1].db-wal").exists()
    assert (extracted / "state" / "cache1.db").read_text(encoding="utf-8") == "plain-cache1-unchanged"
    assert (extracted / "state" / "cash").read_text(encoding="utf-8") == "plain-cash-unchanged"
    members = subprocess.run(
        ["tar", "tzf", "-"], input=tar_out, capture_output=True, check=True
    ).stdout.decode().splitlines()
    assert members.count("./state/cash$") == 1
    restored_dollar = sqlite3.connect(extracted / "state" / "cash$")
    try:
        assert restored_dollar.execute("SELECT k FROM t").fetchall() == [("snap-cash",)]
    finally:
        restored_dollar.close()
    restored = sqlite3.connect(extracted / "state" / "cache[1].db")
    try:
        assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert restored.execute("SELECT k FROM cache").fetchall() == [("snap-cache-1",)]
    finally:
        restored.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="needs bsdtar: -s rewrite and ^ anchoring")
def test_var_dalton_tar_retry_on_vanished_file(tmp_path: Path) -> None:
    import tarfile

    assert backup_lib.should_retry_tar_failure(1, "1 0 0 0") is True
    assert backup_lib.should_retry_tar_failure(1, "141 1 1 7") is False
    assert backup_lib.should_retry_tar_failure(1, "0 0 0 1") is False
    assert backup_lib.should_retry_tar_failure(2, "1 0 0 0") is False
    summary = backup_lib.summarize_tar_stderr(
        "tar: ./vanished.tmp: Cannot stat: No such file or directory\ntar: Error exit delayed\n"
    )
    assert "\n" not in summary
    assert "./vanished.tmp: Cannot stat: No such file or directory" in summary
    assert len(backup_lib.summarize_tar_stderr("x" * 500, max_chars=50)) == 50

    home_dir = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    drive_store = tmp_path / "drive_store"
    ext_mount = tmp_path / "ext_ssd"
    prod_dir = tmp_path / "prod"
    prod_var = prod_dir / "var"
    for d in (home_dir, fake_bin, drive_store, ext_mount, prod_var):
        d.mkdir(parents=True)

    (prod_dir / ".env").write_text("REDIS_PASSWORD=pw\n", encoding="utf-8")
    (prod_var / "keep.txt").write_text("hello-var\n", encoding="utf-8")

    minio_tar = tmp_path / "minio.tar.gz"
    minio_inner = tmp_path / "m.txt"
    minio_inner.write_text("m", encoding="utf-8")
    with tarfile.open(minio_tar, "w:gz") as tf:
        tf.add(minio_inner, arcname="m.txt")

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env python3
import pathlib, sys
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd in ("info", "volume", "inspect", "ps"):
    sys.exit(0)
if cmd == "run":
    sys.stdout.buffer.write(pathlib.Path({str(minio_tar)!r}).read_bytes())
    sys.exit(0)
if cmd == "exec":
    sys.stdin.read()
    sys.stdout.buffer.write(b"REDIS0011ok")
    sys.exit(0)
sys.exit(1)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    about_file = tmp_path / "about.json"
    about_file.write_text('{"free": 100000000}\n', encoding="utf-8")
    proxy_check_file = tmp_path / "proxy_seen.txt"
    rcat_sizes_log = tmp_path / "rcat_sizes.jsonl"
    _write_fake_rclone(
        fake_bin,
        drive_store=drive_store,
        about_file=about_file,
        proxy_check_file=proxy_check_file,
        rcat_sizes_log=rcat_sizes_log,
    )

    real_tar = shutil.which("tar") or "/usr/bin/tar"
    tar_counter = tmp_path / "tar_count.txt"
    fake_tar = fake_bin / "tar"
    fake_tar.write_text(
        f"""#!/usr/bin/env python3
import os, pathlib, subprocess, sys
cnt_path = pathlib.Path({str(tar_counter)!r})
count = int(cnt_path.read_text()) if cnt_path.exists() else 0
count += 1
cnt_path.write_text(str(count))
fail_times = int(os.environ.get("FAKE_TAR_FAIL_COUNT", "0"))
if count <= fail_times:
    sys.stdout.buffer.write(b"partial-tar-bytes")
    sys.stdout.buffer.flush()
    sys.stderr.write("tar: ./ephemeral.tmp: Cannot stat: No such file or directory\\n")
    sys.exit(1)
os.execv({real_tar!r}, ["tar", *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    fake_tar.chmod(0o755)

    key_file = home_dir / ".config" / "dalton" / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "alerts.log"
    log_file = home_dir / "Library" / "Logs" / "backup.log"
    env_file = tmp_path / "offsite-backup.env"
    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=tmp_path / "repo",
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=prod_dir,
        prod_var=prod_var,
        user_config=home_dir / ".config" / "dalton",
        launchagents=home_dir / "Library" / "LaunchAgents",
        backup_scripts=tmp_path / "bs",
        colima=home_dir / ".colima",
        ext_mount=ext_mount,
    )
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f'DALTON_PROD_ENV_FILE="{prod_dir / ".env"}"\n')

    delete_log = tmp_path / "rclone_deletes.log"
    mounted_py = _make_mounted_python_wrapper(tmp_path, ext_mount)
    base_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home_dir),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
        "DALTON_REPO_PYTHON": str(mounted_py),
        "FAKE_RCLONE_DELETE_LOG": str(delete_log),
    }

    # Case 1: First tar fails, second succeeds -> zero alerts, .partial deleted once, retry reason logged.
    res_retry_ok = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
        env={**base_env, "FAKE_TAR_FAIL_COUNT": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_retry_ok.returncode == 0, res_retry_ok.stderr
    assert not alerts_file.exists()
    deleted_objs = delete_log.read_text(encoding="utf-8").splitlines()
    assert len(deleted_objs) == 1
    assert "var-dalton-" in deleted_objs[0] and ".partial-" in deleted_objs[0]
    today_str = dt.datetime.now().strftime("%Y-%m-%d")
    week_drive = drive_store / "DaltonBackup" / "weekly" / today_str
    assert list(week_drive.glob("*.partial-*")) == []
    assert (week_drive / f"var-dalton-{today_str}.tar.gz.enc").is_file()
    log_text = log_file.read_text(encoding="utf-8")
    assert "retrying once: tar: ./ephemeral.tmp: Cannot stat: No such file or directory" in log_text
    assert list((ext_mount / "Dalton" / "backups" / ".tmp").glob("*")) == []

    # Case 2: Both attempts fail -> .partial deleted twice more, VAR_DALTON_BACKUP_FAILED alert emitted.
    tar_counter.unlink(missing_ok=True)
    delete_log.unlink(missing_ok=True)
    res_retry_fail = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
        env={**base_env, "FAKE_TAR_FAIL_COUNT": "2"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_retry_fail.returncode != 0
    assert "VAR_DALTON_BACKUP_FAILED" in alerts_file.read_text(encoding="utf-8")
    assert len(delete_log.read_text(encoding="utf-8").splitlines()) == 2
    assert list(week_drive.glob("*.partial-*")) == []
    assert list((ext_mount / "Dalton" / "backups" / ".tmp").glob("*")) == []

    # Case 3: Downstream rclone rcat failure on var-dalton must alert immediately WITHOUT retrying tar.
    alerts_file.unlink(missing_ok=True)
    tar_counter.unlink(missing_ok=True)
    delete_log.unlink(missing_ok=True)
    res_rcat_fail = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
        env={
            **base_env,
            "FAKE_TAR_FAIL_COUNT": "0",
            "FAKE_RCLONE_FAIL_MATCH": "var-dalton-",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_rcat_fail.returncode != 0
    assert int(tar_counter.read_text(encoding="utf-8")) == 1
    assert "VAR_DALTON_BACKUP_FAILED" in alerts_file.read_text(encoding="utf-8")
    assert len(delete_log.read_text(encoding="utf-8").splitlines()) == 1
    assert list(week_drive.glob("*.partial-*")) == []
    assert list((ext_mount / "Dalton" / "backups" / ".tmp").glob("*")) == []

    # Case 4: SQLite snapshot failure emits VAR_DALTON_BACKUP_FAILED and cleans up staging.
    alerts_file.unlink(missing_ok=True)
    tar_counter.unlink(missing_ok=True)
    (prod_var / "corrupt.sqlite").write_bytes(backup_lib.SQLITE_FILE_HEADER + b"\xff" * 200)
    res_snap_fail = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_data_backup.sh")],
        env={**base_env, "FAKE_TAR_FAIL_COUNT": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_snap_fail.returncode != 0
    assert "VAR_DALTON_BACKUP_FAILED" in alerts_file.read_text(encoding="utf-8")
    assert "SQLite snapshot failed" in alerts_file.read_text(encoding="utf-8")
    assert list((ext_mount / "Dalton" / "backups" / ".tmp").glob("*")) == []


def test_config_and_data_var_excludes_match_env_example() -> None:
    """Default exclude lists in shell scripts must match offsite-backup.env.example."""
    import re

    assert "openclaw-data/skills/*/temp" in backup_lib.DEFAULT_DATA_VAR_EXCLUDES
    assert "run-secrets" in backup_lib.DEFAULT_DATA_VAR_EXCLUDES

    cfg_sh = (OPS_DIR / "dalton_config_git_backup.sh").read_text(encoding="utf-8")
    data_sh = (OPS_DIR / "dalton_data_backup.sh").read_text(encoding="utf-8")
    env_ex = (OPS_DIR / "offsite-backup.env.example").read_text(encoding="utf-8")

    m_cfg_sh = re.search(
        r'^VAR_EXCLUDES="\$\{DALTON_CONFIG_VAR_EXCLUDES:-([^"]+)\}"',
        cfg_sh,
        re.MULTILINE,
    )
    m_cfg_ex = re.search(
        r'^DALTON_CONFIG_VAR_EXCLUDES="([^"]+)"',
        env_ex,
        re.MULTILINE,
    )
    assert m_cfg_sh is not None
    assert m_cfg_ex is not None
    assert m_cfg_sh.group(1) == m_cfg_ex.group(1)
    assert m_cfg_sh.group(1) == ",".join(backup_lib.DEFAULT_CONFIG_VAR_EXCLUDES)

    m_data_sh = re.search(
        r'^DATA_VAR_EXCLUDES="\$\{DALTON_DATA_VAR_EXCLUDES:-([^"]+)\}"',
        data_sh,
        re.MULTILINE,
    )
    m_data_ex = re.search(
        r'^DALTON_DATA_VAR_EXCLUDES="([^"]+)"',
        env_ex,
        re.MULTILINE,
    )
    assert m_data_sh is not None
    assert m_data_ex is not None
    assert m_data_sh.group(1) == m_data_ex.group(1)
    assert m_data_sh.group(1) == ",".join(backup_lib.DEFAULT_DATA_VAR_EXCLUDES)


def test_ops_shell_scripts_bash_syntax() -> None:
    """All shell scripts in scripts/dalton/ops pass `bash -n` syntax validation."""
    scripts = sorted(OPS_DIR.glob("*.sh"))
    assert scripts
    for script in scripts:
        proc = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"
