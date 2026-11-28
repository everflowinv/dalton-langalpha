import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/dalton/seed_openclaw_data.sh"
DATA_ASSETS_DIR = Path(__file__).resolve().parents[3] / "scripts/dalton/data_assets"

_EVERFLOW_PREFLIGHT_PLACEHOLDERS = {
    "DALTON_DATA_TASK_BBG_HOST": "bbg@test.example",
    "DALTON_DATA_TASK_BBG_CSV_REMOTE": "C:/test/bbg.csv",
    "DALTON_DATA_TASK_BBG_XLSX_REMOTE": "C:/test/bbg.xlsx",
    "DALTON_DATA_TASK_BBG_REFRESH_CMD": "cmd /c test.bat",
    "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID": "oc_placeholder_tracking",
    "DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID": "oc_placeholder_semi",
    "DALTON_DATA_TASK_FEISHU_ASIA_TARGET": "chat:oc_placeholder_asia",
    "DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET": "oc_placeholder_megacap",
    "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID": "oc_placeholder_sentiment",
    "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET": "user:ou_placeholder_ashare",
    "DALTON_DATA_TASK_DISCORD_DM_TARGET": "user:100000000000000001",
    "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL": "channel:100000000000000002",
    "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL": "channel:100000000000000003",
    "DALTON_SEC_USER_AGENT": "TestBot test@example.com",
    "DALTON_DATA_TASK_GS_PB_PYTHON": "/opt/test/gs-pb/bin/python",
}


def _write_preflight_config(target: Path) -> None:
    cfg = target / "config" / "data_tasks.env"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "\n".join(f"{k}={v}" for k, v in _EVERFLOW_PREFLIGHT_PLACEHOLDERS.items()) + "\n",
        encoding="utf-8",
    )


def _seed_env() -> dict[str, str]:
    return dict(os.environ)


class SeedOpenClawDataTests(unittest.TestCase):
    def test_preview_preserves_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", "--target", str(target)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Mode: DRY-RUN", result.stdout)
            self.assertFalse(target.exists())

    def test_applies_digest_and_kweb_data_assets_to_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            (target / "scripts").mkdir(parents=True)
            (target / "skills/kweb-dashboard/scripts").mkdir(parents=True)
            (target / "scripts/sentiment_dashboard_digest.sh").write_text(
                "#!/usr/bin/env bash\necho stale upload COS\n", encoding="utf-8"
            )
            (target / "skills/kweb-dashboard/scripts/kweb_dashboard_cli.py").write_text(
                "# stale kweb cli\n", encoding="utf-8"
            )
            _write_preflight_config(target)

            result = subprocess.run(
                ["bash", str(SCRIPT), "--apply", "--target", str(target)],
                env=_seed_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            self.assertEqual(
                (target / "scripts/sentiment_dashboard_digest.sh").read_text(encoding="utf-8"),
                (DATA_ASSETS_DIR / "scripts/sentiment_dashboard_digest.sh").read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "cos_upload",
                (target / "scripts/sentiment_dashboard_digest.sh").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (target / "skills/kweb-dashboard/scripts/kweb_dashboard_cli.py").read_text(
                    encoding="utf-8"
                ),
                (DATA_ASSETS_DIR / "skills/kweb-dashboard/scripts/kweb_dashboard_cli.py").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                "--no-upload",
                (target / "skills/kweb-dashboard/scripts/kweb_dashboard_cli.py").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                (target / "skills/kweb-dashboard/SKILL.md").read_text(encoding="utf-8"),
                (DATA_ASSETS_DIR / "skills/kweb-dashboard/SKILL.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "deploy --no-upload",
                (target / "skills/kweb-dashboard/SKILL.md").read_text(encoding="utf-8"),
            )

    def test_syncs_antigravity_series_module_and_exports_json_catalog(self):
        import json

        repo_root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"

            dry = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", "--target", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertIn("skills/model-usage-cost/scripts/antigravity_series.py", dry.stdout)
            self.assertIn("skills/model-usage-cost/references/antigravity_series.json", dry.stdout)

            _write_preflight_config(target)
            applied = subprocess.run(
                ["bash", str(SCRIPT), "--apply", "--target", str(target)],
                env=_seed_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)

            synced_mod = target / "skills/model-usage-cost/scripts/antigravity_series.py"
            synced_json = target / "skills/model-usage-cost/references/antigravity_series.json"
            self.assertTrue(synced_mod.is_file())
            self.assertTrue(synced_json.is_file())
            self.assertEqual(
                synced_mod.read_text(encoding="utf-8"),
                (repo_root / "src/dalton/llm/antigravity_series.py").read_text(encoding="utf-8"),
            )
            catalog = json.loads(synced_json.read_text(encoding="utf-8"))
            self.assertIsInstance(catalog, list)
            series_ids = {entry["series_id"] for entry in catalog}
            self.assertEqual(len(catalog), 7)
            self.assertIn("antigravity-oauth:gemini-4-neon", series_ids)
            self.assertIn("antigravity-oauth:claude-opus-5-5", series_ids)
            self.assertIn("antigravity-oauth:gpt-oss-120b", series_ids)

    def test_offline_catalog_drops_top_level_retired_entries_like_backend(self):
        import json
        import sys

        repo_root = Path(__file__).resolve().parents[3]
        skill_scripts = repo_root / "scripts/dalton/data_assets/skills/model-usage-cost/scripts"
        sys.path.insert(0, str(skill_scripts))
        try:
            import usage_sources
        finally:
            if sys.path and sys.path[0] == str(skill_scripts):
                sys.path.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_overlay = json.loads((repo_root / "src/dalton/llm/native_overlay.json").read_text(encoding="utf-8"))
            ov1 = root / "native_overlay.json"
            ov1.write_text(json.dumps(base_overlay), encoding="utf-8")
            # Retire the sole variant of gpt-oss-120b and one variant (xhigh) of gemini-4-neon
            ov2 = root / "native_sync_overlay.json"
            ov2.write_text(
                json.dumps({
                    "models": {},
                    "retired": [
                        {"key": "gpt-oss-120b-medium-oauth", "reason": "pm-retired"},
                        {"key": "gemini-4-neon-xhigh-oauth", "reason": "pm-retired"},
                    ],
                }),
                encoding="utf-8",
            )

            offline_catalog = usage_sources.build_antigravity_series_from_overlays(
                (ov1, ov2),
                dalton_yaml_path=repo_root / "src/dalton/dalton.yaml",
            )
            by_id = {entry["series_id"]: entry for entry in offline_catalog}
            self.assertNotIn("antigravity-oauth:gpt-oss-120b", by_id)
            self.assertIn("antigravity-oauth:gemini-4-neon", by_id)
            self.assertNotIn("xhigh", by_id["antigravity-oauth:gemini-4-neon"]["efforts"])
            self.assertEqual(by_id["antigravity-oauth:gemini-4-neon"]["efforts"], ["low", "medium", "high"])

            # Verify seed_openclaw_data.sh uses the exact same shared helper
            script_text = SCRIPT.read_text(encoding="utf-8")
            self.assertIn("build_antigravity_series_from_overlays", script_text)

    def test_cleanup_retired_cos_subcommand_dry_run_apply_and_containment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "openclaw-data"
            outside = root / "outside"
            outside.mkdir()
            outside_keep = outside / "do_not_delete.txt"
            outside_keep.write_text("precious\n", encoding="utf-8")

            sent_cos = target / "scripts/sentiment_dashboard_cos_upload.py"
            kweb_cos = target / "skills/kweb-dashboard/scripts/kweb_cos_upload.py"
            kweb_cli = target / "skills/kweb-dashboard/scripts/kweb_dashboard_cli.py"
            cos_venv = target / "state/sentiment-dashboard/cos-venv"
            cos_venv_py = cos_venv / "bin/python"
            keep_state = target / "state/sentiment-dashboard/latest.json"

            sent_cos.parent.mkdir(parents=True)
            kweb_cos.parent.mkdir(parents=True)
            cos_venv_py.parent.mkdir(parents=True)

            sent_cos.write_text("# old cos upload\n", encoding="utf-8")
            kweb_cos.write_text("# old kweb cos upload\n", encoding="utf-8")
            kweb_cli.write_text("# stale kweb cli\n", encoding="utf-8")
            cos_venv_py.write_text("#!/bin/sh\n", encoding="utf-8")
            keep_state.write_text('{"ok": true}\n', encoding="utf-8")

            # 1. Default (no --apply) is dry-run: prints target paths, deletes/writes nothing.
            dry = subprocess.run(
                ["bash", str(SCRIPT), "cleanup-retired-cos", "--target", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertIn("DRY-RUN", dry.stdout)
            self.assertIn("sentiment_dashboard_cos_upload.py", dry.stdout)
            self.assertIn("kweb_cos_upload.py", dry.stdout)
            self.assertIn("cos-venv", dry.stdout)
            self.assertIn("kweb_dashboard_cli.py", dry.stdout)
            self.assertTrue(sent_cos.is_file())
            self.assertTrue(kweb_cos.is_file())
            self.assertTrue(cos_venv.is_dir())
            self.assertEqual(kweb_cli.read_text(encoding="utf-8"), "# stale kweb cli\n")

            # 2. --apply removes ONLY the three retired COS items and syncs kweb_dashboard_cli.py.
            applied = subprocess.run(
                ["bash", str(SCRIPT), "cleanup-retired-cos", "--apply", "--target", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            self.assertFalse(sent_cos.exists())
            self.assertFalse(kweb_cos.exists())
            self.assertFalse(cos_venv.exists())
            self.assertTrue(keep_state.is_file())
            self.assertEqual(
                kweb_cli.read_text(encoding="utf-8"),
                (DATA_ASSETS_DIR / "skills/kweb-dashboard/scripts/kweb_dashboard_cli.py").read_text(
                    encoding="utf-8"
                ),
            )

            # 3. Path containment guard refuses symlinked ancestor pointing outside target.
            escape_target = root / "escape-target"
            escape_target.mkdir()
            (escape_target / "scripts").symlink_to(outside)
            escaped = subprocess.run(
                ["bash", str(SCRIPT), "cleanup-retired-cos", "--apply", "--target", str(escape_target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(escaped.returncode, 0)
            self.assertIn("outside target", escaped.stderr)
            self.assertTrue(outside_keep.is_file())
