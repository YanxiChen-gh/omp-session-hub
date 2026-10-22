"""Exercise a real per-user LaunchAgent on macOS runners."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


@unittest.skipUnless(sys.platform == "darwin", "Native macOS launchd integration")
class LaunchdTests(unittest.TestCase):
    def test_installed_service_serves_health_and_stops_without_losing_configuration(self):
        domain = "gui/%s" % os.getuid()
        label = domain + "/io.github.omp-session-hub.hub"
        if subprocess.run(["/bin/launchctl", "print", domain], capture_output=True).returncode:
            self.skipTest("No logged-in GUI launchd domain")
        if subprocess.run(["/bin/launchctl", "print", label], capture_output=True).returncode == 0:
            self.skipTest("An existing hub LaunchAgent must not be disturbed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="hub-mac-", dir="/tmp") as temporary:
            home = Path(temporary)
            environment = {**os.environ, "HOME": temporary, "XDG_STATE_HOME": str(home / "state")}
            adapter = home / "adapter.py"
            adapter.write_text('print(\'{"repositories":[],"workspaces":[]}\')\n')
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            subprocess.run(["sh", str(root / "install.sh"), "--source", str(root), "--revision", "2" * 40], env=environment, check=True, capture_output=True)
            app = str(home / ".local/bin/omp-session-hub")
            try:
                subprocess.run([app, "service", "install", "hub", "--adapter", str(adapter), "--port", str(port)], env=environment, check=True, capture_output=True, timeout=30)
                deadline = time.monotonic() + 10
                health = None
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:%s/healthz" % port, timeout=1) as response:
                            health = json.load(response)
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.1)
                state = home / "state/omp-session-hub"
                diagnostics = {}
                if health is None:
                    for name in ("hub.json", "hub.log", "hub.log.1"):
                        path = state / name
                        if path.exists():
                            diagnostics[name] = path.read_text(errors="replace")[-8000:]
                    launchd = subprocess.run(
                        ["/bin/launchctl", "print", label], capture_output=True, text=True,
                    ).stdout
                    diagnostics["launchd"] = launchd[-8000:]
                self.assertEqual(health, {"ok": True}, diagnostics)
                subprocess.run([app, "service", "ensure", "hub"], env=environment, check=True, capture_output=True, timeout=15)
                subprocess.run([app, "service", "stop", "hub"], env=environment, check=True, capture_output=True, timeout=20)
                status = subprocess.run([app, "service", "status", "hub"], env=environment, capture_output=True, text=True)
                self.assertEqual(status.returncode, 1)
                self.assertFalse(json.loads(status.stdout)["running"])
                self.assertTrue((home / "state/omp-session-hub/hub.json").is_file())
            finally:
                subprocess.run([app, "service", "stop", "hub"], env=environment, capture_output=True, timeout=20)


if __name__ == "__main__":
    unittest.main()
