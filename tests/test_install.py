import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REVISION = "1" * 40


class InstallTests(unittest.TestCase):
    def test_installed_zipapp_runs_and_reinstall_preserves_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {**os.environ, "HOME": directory}
            command = ["sh", str(ROOT / "install.sh"), "--source", str(ROOT), "--revision", REVISION]
            subprocess.run(command, env=env, check=True, capture_output=True)
            executable = home / ".local/bin/omp-session-hub"
            result = subprocess.run([str(executable), "--version"], env=env, check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "0.1.0")
            stopped = subprocess.run([str(executable), "service", "status", "hub"], env=env, capture_output=True)
            self.assertEqual(stopped.returncode, 1)
            invalid = subprocess.run([str(executable), "service", "install", "hub", "--adapter", str(home / "missing.py")], env=env, capture_output=True)
            self.assertEqual(invalid.returncode, 1)
            installed = executable.resolve()
            before = installed.stat().st_mtime_ns
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertEqual(executable.resolve(), installed)
            self.assertEqual(installed.stat().st_mtime_ns, before)

    def test_existing_user_executable_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / ".local/bin/omp-session-hub"
            executable.parent.mkdir(parents=True)
            executable.write_text("personal executable\n")
            result = subprocess.run(["sh", str(ROOT / "install.sh"), "--source", str(ROOT), "--revision", REVISION], env={**os.environ, "HOME": directory}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(executable.read_text(), "personal executable\n")


if __name__ == "__main__":
    unittest.main()
