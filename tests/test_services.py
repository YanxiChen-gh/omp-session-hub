import contextlib
import http.client
import io
import os
from pathlib import Path
import plistlib
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from omp_hub import services


class ServiceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base)})
        self.environment.start()
        self.directory = services._state_dir()
        self.adapter = self.base / "adapter.py"
        self.adapter.write_text("print('{}')\n", encoding="utf-8")

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return services.service_main(args)

    def test_install_and_ensure_preserve_nondefault_saved_settings(self):
        with mock.patch.object(services, "_ensure"), mock.patch.object(services, "_stop"):
            self.assertEqual(self.invoke(["install", "hub", "--adapter", str(self.adapter), "--port", "5151"]), 0)
            original = services._read_private(self.directory / "hub.json")
            self.assertEqual(self.invoke(["ensure", "hub"]), 0)
            self.assertEqual(services._read_private(self.directory / "hub.json"), original)
            self.assertEqual(original["options"]["port"], 5151)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.directory / "hub.json").stat().st_mode), 0o600)

    def test_zipapp_symlink_and_interpreter_are_pinned(self):
        archive = self.base / "version" / "app.pyz"
        archive.parent.mkdir()
        archive.write_bytes(b"zipapp")
        entry = self.base / "cli"
        entry.symlink_to(archive)
        with mock.patch.object(sys, "argv", [str(entry)]):
            command, environment = services._execution()
        self.assertEqual(command, [str(Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()), str(archive.resolve())])
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["PATH"].split(os.pathsep)[0], str(Path.home() / ".local" / "bin"))

    def test_unsafe_state_or_config_is_rejected_without_chmod(self):
        os.chmod(str(self.directory), 0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            services._state_dir()
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o755)
        os.chmod(str(self.directory), 0o700)
        target = self.base / "unrelated.json"
        target.write_text("{}", encoding="utf-8")
        (self.directory / "hub.json").symlink_to(target)
        self.assertEqual(self.invoke(["ensure", "hub"]), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_lock_excludes_second_owner_and_stop_never_uses_pid_files(self):
        descriptor = services._lock(self.directory, "hub")
        try:
            self.assertIsNone(services._lock(self.directory, "hub"))
        finally:
            os.close(descriptor)
        (self.directory / "hub.pid").write_text(str(os.getpid()), encoding="utf-8")
        with mock.patch.object(services.platform, "system", return_value="Linux"), mock.patch.object(os, "kill") as kill:
            services._stop(self.directory, "hub")
        kill.assert_not_called()
        self.assertTrue((self.directory / "hub.pid").exists())

    def test_ensure_running_service_does_not_spawn_duplicate(self):
        with mock.patch.object(services, "_control", return_value={"running": True}), \
             mock.patch.object(subprocess, "Popen") as spawn:
            services._ensure(self.directory, "hub")
        spawn.assert_not_called()

    def test_unmanaged_launchagent_is_not_replaced_or_stopped(self):
        with mock.patch.object(Path, "home", return_value=self.base):
            path = services._agent_path("gateway")
            path.write_bytes(plistlib.dumps({"Label": services.LABEL_PREFIX + "gateway", "ProgramArguments": ["/unrelated"]}))
            with mock.patch.object(services, "_launchctl") as launchctl:
                with self.assertRaisesRegex(ValueError, "unmanaged"):
                    services._install_agent("gateway")
                launchctl.assert_not_called()


@unittest.skipUnless(sys.platform.startswith("linux"), "Real Linux supervisor lifecycle")
class LinuxSupervisorTests(unittest.TestCase):
    @contextlib.contextmanager
    def listener_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / ".local" / "state" / "omp-session-hub"
            directory.mkdir(parents=True, mode=0o700)
            adapter = home / "adapter.py"
            adapter.write_text("print('{}')\n", encoding="utf-8")
            backend = home / "backend.py"
            backend.write_text(
                "import os, sys\n"
                "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        data = str(os.getpid()).encode('ascii')\n"
                "        self.send_response(200)\n"
                "        self.send_header('Content-Length', str(len(data)))\n"
                "        self.end_headers()\n"
                "        self.wfile.write(data)\n"
                "    def log_message(self, *args): pass\n"
                "HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()\n",
                encoding="utf-8",
            )
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            source = str(Path(services.__file__).resolve().parent.parent)
            config = {
                "command": [sys.executable, str(backend), str(port)],
                "environment": {"PATH": "/saved/installation/path", "PYTHONPATH": source},
                "options": {"port": port, "adapter": str(adapter.resolve())},
            }
            services._write_private(directory / "hub.json", config)
            environment = os.environ.copy()
            environment.update({"HOME": str(home), "XDG_STATE_HOME": str(directory.parent),
                                "PYTHONPATH": source})
            supervisor_code = "from pathlib import Path; import sys; from omp_hub.services import _supervise; sys.exit(_supervise(Path(sys.argv[1]), 'hub'))"
            process = subprocess.Popen(
                [sys.executable, "-c", supervisor_code, str(directory)],
                env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                self.wait_for(lambda: self.listener_pid(port))
                yield directory, port, environment, process, config
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=7)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                subprocess.run(
                    [sys.executable, "-m", "omp_hub", "service", "stop", "hub"],
                    env=environment, capture_output=True, timeout=10,
                )

    def listener_pid(self, port):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            return int(response.read())
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            connection.close()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail("Service lifecycle did not reach the expected state")

    def test_ensure_and_start_preserve_live_child_across_caller_path_changes(self):
        with self.listener_service() as (directory, port, environment, process, original):
            pid = self.listener_pid(port)
            for action, arguments in (
                ("ensure", []),
                ("start", []),
                ("ensure", ["--adapter", original["options"]["adapter"], "--port", str(port)]),
            ):
                caller = dict(environment, PATH="/different/caller/path/" + action)
                result = subprocess.run(
                    [sys.executable, "-m", "omp_hub", "service", action, "hub"] + arguments,
                    env=caller, capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(self.listener_pid(port), pid)
                self.assertIsNone(process.poll())
                self.assertEqual(services._read_private(directory / "hub.json"), original)

    def test_killed_supervisor_reaps_child_and_ensure_recovers_without_orphan_listener(self):
        with self.listener_service() as (directory, port, environment, process, original):
            old_child = self.listener_pid(port)
            process.kill()
            process.wait(timeout=3)
            self.wait_for(lambda: self.listener_pid(port) is None)
            status_path = Path("/proc") / str(old_child) / "stat"

            def child_dead():
                try:
                    return status_path.read_text().rsplit(")", 1)[1].split()[0] == "Z"
                except (FileNotFoundError, ProcessLookupError):
                    return True

            self.wait_for(child_dead)
            result = subprocess.run(
                [sys.executable, "-m", "omp_hub", "service", "ensure", "hub"],
                env=environment, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            new_child = self.wait_for(lambda: self.listener_pid(port))
            self.assertNotEqual(new_child, old_child)
            result = subprocess.run(
                [sys.executable, "-m", "omp_hub", "service", "stop", "hub"],
                env=environment, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIsNone(self.listener_pid(port))
            self.assertEqual(services._read_private(directory / "hub.json"), original)

    def test_restart_single_instance_private_bounded_logs_and_identity_safe_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "state"
            directory.mkdir(mode=0o700)
            marker = directory / "starts"
            code = ("from pathlib import Path; import sys; "
                    "p=Path(sys.argv[1]); "
                    "f=p.open('a'); f.write('started\\n'); f.close(); "
                    "print('x'*700000, flush=True)")
            services._write_private(directory / "hub.json", {
                "command": [sys.executable, "-c", code, str(marker)], "environment": {},
            })
            supervisor_code = "from pathlib import Path; import sys; from omp_hub.services import _supervise; sys.exit(_supervise(Path(sys.argv[1]), 'hub'))"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(services.__file__).resolve().parent.parent)
            process = subprocess.Popen([sys.executable, "-c", supervisor_code, str(directory)],
                                       env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                status = None
                while time.monotonic() < deadline:
                    status = services._control(directory, "hub", "status")
                    if status:
                        break
                    time.sleep(0.05)
                self.assertTrue(status and status["running"])
                duplicate = subprocess.run([sys.executable, "-c", supervisor_code, str(directory)],
                                           env=environment, capture_output=True, timeout=3)
                self.assertEqual(duplicate.returncode, 0)
                while time.monotonic() < deadline:
                    if marker.exists() and len(marker.read_text(encoding="utf-8").splitlines()) >= 3:
                        break
                    time.sleep(0.1)
                self.assertGreaterEqual(len(marker.read_text(encoding="utf-8").splitlines()), 3)
                for path in directory.glob("*.log*"):
                    self.assertLessEqual(path.stat().st_size, services.LOG_LIMIT)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertTrue((directory / "hub.log.1").exists())
                with mock.patch.object(services.platform, "system", return_value="Linux"):
                    services._stop(directory, "hub")
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertTrue((directory / "hub.json").exists())
                self.assertIsNone(services._control(directory, "hub", "status"))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=7)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    unittest.main()
