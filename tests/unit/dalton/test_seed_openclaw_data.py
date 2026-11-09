from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/dalton/seed_openclaw_data.sh"


class SeedOpenClawDataTests(unittest.TestCase):
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
