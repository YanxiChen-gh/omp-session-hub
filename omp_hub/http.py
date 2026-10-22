"""HTTP boundary shared by the loopback dashboard."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.parse import urlsplit

MAX_BODY = 32 * 1024
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Frame-Options": "DENY",
}


class RequestError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def server_bind(self):
        # Both listeners use numeric addresses; startup must not wait for DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = "OMP-Hub"
    sys_version = ""

    def log_message(self, *args):
        # Requests can contain control capabilities; never put them in logs.
        pass

    def send_data(self, status, data, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, status, data):
        self.send_data(status, json.dumps(data, ensure_ascii=True).encode("utf-8"))

    def send_error(self, code, message=None, explain=None):
        self.send_json(code, {"error": message or "Request rejected"})

    def boundary(self):
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise RequestError(400, "Invalid request target")
        target = urlsplit(self.path)
        if target.scheme or target.netloc or target.fragment or target.query:
            raise RequestError(400, "Invalid request target")
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_address[1]
        allowed = {"127.0.0.1:%s" % port, "localhost:%s" % port}
        if len(hosts) != 1 or hosts[0] not in allowed:
            raise RequestError(403, "Host is not allowed")
        origin = self.headers.get_all("Origin", [])
        if origin and (len(origin) != 1 or origin[0] != "http://" + hosts[0]):
            raise RequestError(403, "Origin is not allowed")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError(403, "Cross-site requests are not allowed")
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "Transfer encoding is not supported")
        return target.path

    def json_body(self):
        if self.headers.get("X-OMP-Hub") != "1":
            raise RequestError(403, "Missing request marker")
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "Expected application/json")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise RequestError(411, "A content length is required")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise RequestError(413, "Request is too large")
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise RequestError(400, "Invalid JSON")
        if not isinstance(body, dict):
            raise RequestError(400, "Expected a JSON object")
        return body

    def do_OPTIONS(self):
        self.send_error(405, "Cross-origin access is not supported")
