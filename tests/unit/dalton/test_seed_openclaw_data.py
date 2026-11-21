from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/dalton/seed_openclaw_data.sh"
DATA_ASSETS_DIR = Path(__file__).resolve().parents[3] / "scripts/dalton/data_assets"


class SeedOpenClawDataTests(unittest.TestCase):
    def test_python_launcher_replaces_link_without_changing_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            interpreter = root / "interpreter"
            interpreter.write_text("preserve existing interpreter\n")
            launcher = target / "state/sentiment-dashboard/cos-venv/bin/python"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(interpreter)

            for _ in range(2):
                result = subprocess.run(
                    ["bash", str(SCRIPT), "--apply", "--target", str(target)],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(launcher.is_symlink())
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
                self.assertEqual(
                    launcher.read_text(), '#!/bin/sh\nexec /app/.venv/bin/python "$@"\n'
                )
                self.assertEqual(interpreter.read_text(), "preserve existing interpreter\n")
                self.assertEqual(list(launcher.parent.glob("python.*")), [])
                self.assertTrue((target / "state/a-share-volume-watch").is_dir())

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

    def test_legacy_source_flag_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", "--source", str(root), "--target", str(root / "target")],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--source sync from OpenClaw workspace has been removed", result.stderr)

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

            result = subprocess.run(
                ["bash", str(SCRIPT), "--apply", "--target", str(target)],
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

            applied = subprocess.run(
                ["bash", str(SCRIPT), "--apply", "--target", str(target)],
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
