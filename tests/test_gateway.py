import contextlib
import http.client
import io
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from omp_hub import gateway


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.received.append((self.command, self.path, dict(self.headers), b""))
        if self.path == "/api/large":
            self.send_response(200)
            self.send_header("Content-Length", str(gateway.MAX_RESPONSE + 1))
            self.end_headers()
            return
        if self.path == "/api/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "X-Hop")
            self.send_header("X-Hop", "secret")
            self.end_headers()
            self.wfile.write(b"5\r\nhello\r\n0\r\n\r\n")
            return
        data = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append((self.command, self.path, dict(self.headers), body))
        self.send_response(202)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.upstream.received = []
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        self.gateway = gateway.GatewayServer(("127.0.0.1", 0), self.upstream.server_port)
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()
        self.authority = "127.0.0.1:%s" % self.gateway.server_port

    def tearDown(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.gateway_thread.join()
        self.upstream_thread.join()

    def request(self, method="GET", path="/healthz", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.gateway.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.gateway.server_port), timeout=3) as connection:
            connection.sendall(request.encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, response.read()

    def test_valid_proxy_rewrites_boundary_and_strips_hop_headers(self):
        status, _, body = self.request("POST", "/api/workspaces", "{}", {
            "Origin": "http://" + self.authority,
            "X-OMP-Hub": "1", "Content-Type": "application/json; charset=utf-8",
            "Connection": "X-Hop", "X-Hop": "secret", "Cookie": "private=secret",
        })
        self.assertEqual(status, 202)
        self.assertEqual(body, b"{}")
        _, path, headers, sent = self.upstream.received[-1]
        self.assertEqual(path, "/api/workspaces")
        self.assertEqual(sent, b"{}")
        self.assertEqual(headers["Host"], "127.0.0.1:%s" % self.upstream.server_port)
        self.assertEqual(headers["Origin"], "http://127.0.0.1:%s" % self.upstream.server_port)
        self.assertNotIn("X-Hop", headers)
        self.assertNotIn("Cookie", headers)

    def test_cross_origin_host_and_fetch_site_rejected_before_upstream(self):
        for headers in ({"Host": "attacker.example"}, {"Host": "localhost:%s" % self.gateway.server_port},
                        {"Origin": "https://attacker.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertEqual(self.upstream.received, [])

    def test_absolute_targets_and_non_app_paths_rejected(self):
        for path in ("http://127.0.0.1/healthz", "//attacker.example/healthz", "/private",
                     "/api/../private", "/api/%2e%2e/private", "/api/%00", "/healthz#fragment"):
            with self.subTest(path=path):
                status, _ = self.raw("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % (path, self.authority))
                self.assertEqual(status, 400)
        self.assertEqual(self.upstream.received, [])

    def test_mutations_require_both_json_and_marker(self):
        for headers in ({}, {"X-OMP-Hub": "1"}, {"Content-Type": "application/json"},
                        {"X-OMP-Hub": "1", "Content-Type": "text/plain"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/workspaces", "{}", headers)[0], 403)
        self.assertEqual(self.upstream.received, [])

    def test_ambiguous_framing_and_duplicate_security_headers_rejected(self):
        headers = [
            ("Content-Length: 0\r\nContent-Length: 0", 400),
            ("Transfer-Encoding: chunked", 400),
            ("Host: attacker.example", 403),
            ("Origin: http://%s\r\nOrigin: http://%s" % (self.authority, self.authority), 403),
            ("Content-Length: %s" % (gateway.MAX_REQUEST + 1), 413),
            ("Content-Length: " + "9" * 100, 400),
        ]
        for extra, expected in headers:
            with self.subTest(extra=extra):
                status, _ = self.raw("GET /healthz HTTP/1.1\r\nHost: %s\r\n%s\r\nConnection: close\r\n\r\n" % (self.authority, extra))
                self.assertEqual(status, expected)
        self.assertEqual(self.upstream.received, [])

    def test_chunked_upstream_is_streamed_without_hop_headers(self):
        status, headers, body = self.request(path="/api/chunked")
        self.assertEqual((status, body), (200, b"hello"))
        self.assertNotIn("Transfer-Encoding", headers)
        self.assertNotIn("X-Hop", headers)

    def test_oversized_response_and_missing_forward_are_clear_502(self):
        self.assertEqual(self.request(path="/api/large")[0], 502)
        self.upstream.shutdown()
        self.upstream.server_close()
        status, headers, body = self.request()
        self.assertEqual(status, 502)
        self.assertIn("IDE forward", json.loads(body)["error"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_requests_do_not_log_control_links(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.request(path="/api/jobs/private-capability")
        self.assertNotIn("private-capability", output.getvalue())


class LanSelectionTests(unittest.TestCase):
    def test_only_active_physical_private_interface_is_selected(self):
        def command(args):
            if args[0] == "/sbin/route":
                return "interface: utun3"
            if args[0] == "/usr/sbin/networksetup":
                return "Hardware Port: Ethernet\nDevice: en1"
            if args == ["/sbin/ifconfig", "en0"]:
                return "status: inactive"
            if args == ["/sbin/ifconfig", "en1"]:
                return "status: active"
            if args == ["/usr/sbin/ipconfig", "getifaddr", "en1"]:
                return "192.168.2.4"
            self.fail("Unexpected interface command: %r" % args)
        with mock.patch.object(gateway, "_command", side_effect=command):
            self.assertEqual(gateway.detect_lan_address(), "192.168.2.4")

    def test_public_wildcard_link_local_and_loopback_not_auto_selected(self):
        for address in ("0.0.0.0", "127.0.0.1", "169.254.1.1", "8.8.8.8", "not-an-ip"):
            self.assertIsNone(gateway._address(address))
        self.assertEqual(gateway._address("127.0.0.1", allow_loopback=True), "127.0.0.1")

    def test_auto_rebinds_when_lan_changes_and_closes_on_exit(self):
        first, second = mock.Mock(), mock.Mock()
        with mock.patch.object(gateway.platform, "system", return_value="Darwin"), \
             mock.patch.object(gateway, "detect_lan_address", side_effect=["192.168.1.2", "192.168.1.3"]), \
             mock.patch.object(gateway, "GatewayServer", side_effect=[first, second]) as factory, \
             mock.patch.object(gateway.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
             mock.patch.object(gateway.threading, "Thread"):
            with self.assertRaises(KeyboardInterrupt):
                gateway.run_gateway()
        self.assertEqual(factory.call_args_list, [mock.call(("192.168.1.2", 8787), 4317),
                                                  mock.call(("192.168.1.3", 8787), 4317)])
        first.shutdown.assert_called_once()
        first.server_close.assert_called_once()
        second.shutdown.assert_called_once()
        second.server_close.assert_called_once()

    def test_linux_auto_requires_explicit_address(self):
        with mock.patch.object(gateway.platform, "system", return_value="Linux"):
            with self.assertRaisesRegex(ValueError, "--bind"):
                gateway.run_gateway()


if __name__ == "__main__":
    unittest.main()
