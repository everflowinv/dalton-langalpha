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
