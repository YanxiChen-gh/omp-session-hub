import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from omp_hub.hub import Hub, creation_spec, make_handler
from omp_hub.http import RequestError, Server
from omp_hub.gateway import GatewayServer


class OfflineStartupTests(unittest.TestCase):
    def test_hub_and_gateway_serve_when_reverse_dns_is_unavailable(self):
        with patch("socket.getfqdn", side_effect=OSError("DNS unavailable")):
            upstream = Server(("127.0.0.1", 0), make_handler(object()))
            gateway = GatewayServer(("127.0.0.1", 0), upstream.server_port)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (upstream, gateway)]
        for thread in threads:
            thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=3)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})
            connection.close()
        finally:
            for server in (gateway, upstream):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        class App:
            def create(inner, body):
                self.calls.append(body)
                return {"jobId": "created"}
        self.server = Server(("127.0.0.1", 0), make_handler(App()))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method="GET", path="/healthz", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = json.loads(response.read())
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def test_dns_rebinding_host_cannot_read_dashboard(self):
        status, _, _ = self.request(headers={"Host": "attacker.example:%s" % self.port})
        self.assertEqual(status, 403)

    def test_cross_origin_cannot_create_workspace(self):
        for headers in (
            {"Origin": "https://attacker.example", "X-OMP-Hub": "1"},
            {"Sec-Fetch-Site": "cross-site", "X-OMP-Hub": "1"},
            {},
        ):
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/workspaces", {"Content-Type": "application/json", **headers}, '{}')
                self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_same_origin_json_creation_works(self):
        status, headers, data = self.request("POST", "/api/workspaces", {
            "Content-Type": "application/json", "X-OMP-Hub": "1",
            "Origin": "http://127.0.0.1:%s" % self.port,
        }, '{"repository":"/repo"}')
        self.assertEqual(status, 202)
        self.assertEqual(data["jobId"], "created")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.calls, [{"repository": "/repo"}])

    def test_oversized_or_nonobject_json_cannot_create(self):
        for body, expected in (("[]", 400), ('"' + 'x' * 33000 + '"', 413)):
            status, _, _ = self.request("POST", "/api/workspaces", {"Content-Type": "application/json", "X-OMP-Hub": "1"}, body)
            self.assertEqual(status, expected)
        self.assertEqual(self.calls, [])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile()
        self.hub = Hub(self.file.name)
        self.host = {"instanceId": "abcd1234-host", "generation": 2, "access": "control"}

    def tearDown(self):
        self.file.close()

    def test_stale_selection_cannot_open_successor_session(self):
        with patch.object(self.hub, "sessions", return_value={"sessions": [self.host]}), patch("omp_hub.hub.command_json") as command:
            with self.assertRaises(RequestError) as error:
                self.hub.open_session(self.host["instanceId"], {"generation": 1})
            self.assertEqual(error.exception.status, 409)
            command.assert_not_called()

    def test_rotation_during_link_lookup_cannot_return_successor_capability(self):
        with patch.object(self.hub, "sessions", return_value={"sessions": [self.host]}), patch("omp_hub.hub.command_json", return_value={
            **self.host, "generation": 3, "url": "https://my.omp.sh/#successor-secret",
        }):
            with self.assertRaises(RequestError) as error:
                self.hub.open_session(self.host["instanceId"], {"generation": 2})
            self.assertEqual(error.exception.status, 409)
            self.assertNotIn("secret", error.exception.message)

    def test_nonbrowser_link_rejected(self):
        for url in ("javascript:alert(1)", "http://public.example/#key", "https://user:pass@example.com/#key"):
            with self.subTest(url=url), patch.object(self.hub, "sessions", return_value={"sessions": [self.host]}), patch("omp_hub.hub.command_json", return_value={**self.host, "url": url}):
                with self.assertRaises(RequestError):
                    self.hub.open_session(self.host["instanceId"], {"generation": 2})

    def test_creation_concurrency_is_bounded(self):
        self.hub.jobs.update({str(n): {"status": "preparing"} for n in range(4)})
        with self.assertRaises(RequestError) as error:
            self.hub.create({"repository": "/repo"})
        self.assertEqual(error.exception.status, 429)

    def test_failed_creation_surfaces_adapter_error(self):
        self.hub.jobs["job"] = {"status": "preparing"}
        with patch("omp_hub.hub.command_json", return_value={"error": "Checkout is already managed by Treehouse"}):
            self.hub._create("job", {"repository": "/repo"})
        self.assertEqual(self.hub.job("job"), {"status": "failed", "error": "Checkout is already managed by Treehouse"})

    def test_invalid_creation_combinations_rejected(self):
        for spec in (
            {"workspaceId": "-flag"},
            {"workspaceId": "w1", "repository": "/repo"},
            {"repository": "/repo", "agent": "false"},
            {"repository": "/repo", "agent": False, "prompt": "run"},
            {"repository": "/repo", "cloneRepository": "owner/repo"},
            {"repository": "/repo", "command": "rm -rf /"},
        ):
            with self.subTest(spec=spec), self.assertRaises(RequestError):
                creation_spec(spec)

    def test_workspace_identifiers_are_opaque_not_decimal_numbers(self):
        spec = creation_spec({"workspaceId": "wA"})
        self.assertEqual(spec["workspaceId"], "wA")


if __name__ == "__main__":
    unittest.main()
