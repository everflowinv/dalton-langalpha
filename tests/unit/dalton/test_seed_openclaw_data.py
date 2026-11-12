from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/dalton/seed_openclaw_data.sh"


class SeedOpenClawDataTests(unittest.TestCase):
    def test_python_launcher_replaces_link_without_changing_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "target"
            interpreter = root / "interpreter"
            interpreter.write_text("preserve existing interpreter\n")
            launcher = target / "state/sentiment-dashboard/cos-venv/bin/python"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(interpreter)

            for _ in range(2):
                result = subprocess.run(
                    ["bash", str(SCRIPT), "--apply", "--source", str(source),
                     "--target", str(target)],
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

    def test_linked_skill_is_portable_and_excludes_virtualenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            workshop = root / "workshop"
            workshop.mkdir()
            (workshop / "run.sh").write_text("#!/bin/sh\necho ready\n")
            for excluded in ("venv", ".venv", "cos-venv"):
                (workshop / excluded).mkdir()
                (workshop / excluded / "python").write_text("host executable")
            (source / "skills").mkdir(parents=True)
            (source / "skills/tw-leverage-monitor").symlink_to(
                workshop, target_is_directory=True
            )
            (target / "skills").mkdir(parents=True)
            destination = target / "skills/tw-leverage-monitor"
            destination.symlink_to(root / "unavailable", target_is_directory=True)

            for content in ("#!/bin/sh\necho ready\n", "#!/bin/sh\necho updated\n"):
                (workshop / "run.sh").write_text(content)
                result = subprocess.run(
                    ["bash", str(SCRIPT), "--apply", "--source", str(source),
                     "--target", str(target)],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(destination.is_dir())
                self.assertFalse(destination.is_symlink())
                self.assertEqual((destination / "run.sh").read_text(), content)
                for excluded in ("venv", ".venv", "cos-venv"):
                    self.assertFalse((destination / excluded).exists())

    def test_preview_preserves_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = root / "target"
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", "--source", str(source),
                 "--target", str(target)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(target.exists())

    def test_apply_keeps_existing_state_updates_code_and_fills_gaps(self):
        """Re-seed leaves existing state bytes alone, copies missing state, and updates code."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"

            def write(root_dir, rel, text):
                path = root_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                return path

            write(source, "scripts/a_share_volume_watch.py", "code-from-source-v2\n")
            write(source, "config/a_share_volume_watch.json", "config-from-source-v2\n")
            write(source, "skills/ai-chain/scripts/daily_run.py", "skill-code-from-source-v2\n")
            write(source, "skills/ai-chain/config/name_aliases.yaml", "aliases-from-source-v2\n")
            write(source, "skills/ai-chain/state/ingest_health.json", "skill-state-old\n")
            write(source, "skills/ai-chain/data/tickers_seed.csv", "ticker-data-old\n")
            write(source, "skills/ai-chain/temp/run.json", "temp-old\n")
            write(source, "skills/ai-chain/events/bus.jsonl", "events-old\n")
            write(source, "skills/ai-chain/events/consumers.json", "consumers-filled-in\n")
            write(source, "skills/ai-chain/model_cards/NVDA.yaml", "card-old\n")
            write(source, "state/cn-leverage/pipeline_status.json", "phase1-state-old\n")
            write(source, "state/e_watch_signal_state.json", "e-watch-filled-in\n")
            write(source, "research-outputs/sentiment-dashboard/signals.json", "signals-old\n")
            write(source, "state/a-share-volume-watch/stderr.log", "source-log\n")
            write(source, "state/a-share-volume-watch/baseline.json", "baseline-filled-in\n")
            write(source, "market-memory/2026-11-01.md", "digest-md\n")
            write(source, "market-memory/notes.txt", "not-markdown\n")
            write(source, "market-memory/archive/old.md", "nested-md\n")
            write(source, "skills/market-digest/vectors.db", "vectors-db\n")
            write(source, "skills/market-digest/secrets.json", "do-not-seed\n")
            write(source, "skills/company-filings-alert/state/filings.json", "filings-filled-in\n")
            write(source, "skills/company-filings-alert/state/last_check.json", "bookmark-not-seeded\n")

            write(target, "scripts/a_share_volume_watch.py", "stale-code\n")
            write(target, "config/a_share_volume_watch.json", "stale-config\n")
            write(target, "skills/ai-chain/scripts/daily_run.py", "stale-skill-code\n")
            write(target, "skills/ai-chain/config/name_aliases.yaml", "stale-aliases\n")
            write(target, "skills/ai-chain/state/ingest_health.json", "skill-state-newer\n")
            write(target, "skills/ai-chain/data/tickers_seed.csv", "ticker-data-newer\n")
            write(target, "skills/ai-chain/temp/run.json", "temp-newer\n")
            write(target, "skills/ai-chain/events/bus.jsonl", "events-newer\n")
            write(target, "skills/ai-chain/model_cards/NVDA.yaml", "card-newer\n")
            write(target, "state/cn-leverage/pipeline_status.json", "phase1-state-newer\n")
            write(target, "state/cn-leverage/local_only.json", "keep-local\n")
            write(target, "research-outputs/sentiment-dashboard/signals.json", "signals-newer\n")
            write(target, "state/a-share-volume-watch/stderr.log", "target-log-newer\n")

            result = subprocess.run(
                ["bash", str(SCRIPT), "--apply", "--source", str(source),
                 "--target", str(target)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            def text(rel):
                return (target / rel).read_text(encoding="utf-8")

            self.assertEqual(text("scripts/a_share_volume_watch.py"), "code-from-source-v2\n")
            self.assertEqual(text("config/a_share_volume_watch.json"), "config-from-source-v2\n")
            self.assertEqual(text("skills/ai-chain/scripts/daily_run.py"), "skill-code-from-source-v2\n")
            self.assertEqual(text("skills/ai-chain/config/name_aliases.yaml"), "aliases-from-source-v2\n")
            self.assertEqual(text("skills/ai-chain/state/ingest_health.json"), "skill-state-newer\n")
            self.assertEqual(text("skills/ai-chain/data/tickers_seed.csv"), "ticker-data-newer\n")
            self.assertEqual(text("skills/ai-chain/temp/run.json"), "temp-newer\n")
            self.assertEqual(text("skills/ai-chain/events/bus.jsonl"), "events-newer\n")
            self.assertEqual(text("skills/ai-chain/events/consumers.json"), "consumers-filled-in\n")
            self.assertEqual(text("skills/ai-chain/model_cards/NVDA.yaml"), "card-newer\n")
            self.assertEqual(text("state/cn-leverage/pipeline_status.json"), "phase1-state-newer\n")
            self.assertEqual(text("state/cn-leverage/local_only.json"), "keep-local\n")
            self.assertEqual(text("research-outputs/sentiment-dashboard/signals.json"), "signals-newer\n")
            self.assertEqual(text("state/a-share-volume-watch/stderr.log"), "target-log-newer\n")
            self.assertEqual(text("state/e_watch_signal_state.json"), "e-watch-filled-in\n")
            self.assertEqual(text("state/a-share-volume-watch/baseline.json"), "baseline-filled-in\n")
            self.assertEqual(text("market-memory/2026-11-01.md"), "digest-md\n")
            self.assertEqual(text("skills/market-digest/vectors.db"), "vectors-db\n")
            self.assertEqual(text("skills/company-filings-alert/state/filings.json"), "filings-filled-in\n")
            self.assertFalse((target / "market-memory/notes.txt").exists())
            self.assertFalse((target / "market-memory/archive/old.md").exists())
            self.assertFalse((target / "skills/market-digest/secrets.json").exists())
            self.assertFalse((target / "skills/company-filings-alert/state/last_check.json").exists())
            self.assertTrue((target / "state/a-share-volume-watch").is_dir())
