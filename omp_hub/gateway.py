"""Private-LAN HTTP gateway to a fixed IDE loopback forward."""

import http.client
import ipaddress
import json
import platform
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote, urlsplit

from .http import Server

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
UPSTREAM_TIMEOUT = 15
HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection",
})
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


def _address(value, allow_loopback=False):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return None
    if any(address in network for network in PRIVATE_NETWORKS):
        return str(address)
    if allow_loopback and address.is_loopback:
        return str(address)
    return None


def _command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=3,
                                check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def detect_lan_address():
    """Select an active physical Mac interface, never a wildcard or VPN."""
    route = _command(["/sbin/route", "-n", "get", "default"])
    match = re.search(r"^\s*interface:\s*(en[0-9]+)\s*$", route, re.MULTILINE)
    interfaces = [match.group(1)] if match else []
    interfaces.extend(["en0", "en1"])
    hardware = _command(["/usr/sbin/networksetup", "-listallhardwareports"])
    interfaces.extend(re.findall(r"^\s*Device:\s*(en[0-9]+)\s*$", hardware, re.MULTILINE))
    for interface in dict.fromkeys(interfaces):
        status = _command(["/sbin/ifconfig", interface])
        if not re.search(r"^\s*status:\s*active\s*$", status, re.MULTILINE):
            continue
        address = _address(_command(["/usr/sbin/ipconfig", "getifaddr", interface]))
        if address:
            return address
    return None


def _allowed_target(target):
    if any(ord(char) <= 32 or ord(char) == 127 for char in target):
        return False
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return False
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    path = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or parsed.fragment or "\\" in path:
        return False
    if any(part in (".", "..") for part in path.split("/")):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    return path in ("/", "/app.js", "/style.css", "/healthz") or path.startswith("/api/")


class GatewayServer(Server):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, upstream_port):
        self.upstream_port = upstream_port
        super().__init__(address, GatewayHandler)


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OMP-Hub"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(UPSTREAM_TIMEOUT)

    def log_message(self, format, *args):
        # Requests can contain session capabilities; never log URLs or bodies.
        pass

    def _error(self, status, message):
        data = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(data)

    def _proxy(self):
        raw_target = self.requestline.split()[1]
        if not _allowed_target(raw_target):
            self._error(400, "Only session hub paths and relative request targets are allowed.")
            return
        ip, port = self.server.server_address[:2]
        authority = "%s:%s" % (ip, port)
        allowed_hosts = {authority}
        if port == 80:
            allowed_hosts.add(ip)
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in allowed_hosts:
            self._error(403, "Host must match this gateway's LAN address and port.")
            return
        origins = self.headers.get_all("Origin", [])
        allowed_origins = {"http://" + host for host in allowed_hosts}
        if len(origins) > 1 or (origins and origins[0] not in allowed_origins):
            self._error(403, "Cross-origin requests are not allowed.")
            return
        sites = self.headers.get_all("Sec-Fetch-Site", [])
        if len(sites) > 1 or (sites and sites[0] not in ("same-origin", "none")):
            self._error(403, "Cross-site requests are not allowed.")
            return
        if self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Expect"):
            self._error(400, "Transfer-Encoding and Expect are not supported.")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and (len(lengths[0]) > 10 or not re.fullmatch(r"[0-9]+", lengths[0]))):
            self._error(400, "Invalid Content-Length.")
            return
        length = int(lengths[0]) if lengths else 0
        if length > MAX_REQUEST:
            self._error(413, "Request body exceeds the gateway limit.")
            return
        if self.command == "POST":
            if not urlsplit(self.path).path.startswith("/api/"):
                self._error(405, "Mutations are allowed only on the API.")
                return
            markers = self.headers.get_all("X-OMP-Hub", [])
            types = self.headers.get_all("Content-Type", [])
            if markers != ["1"] or len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
                self._error(403, "Mutations require X-OMP-Hub: 1 and application/json.")
                return
            if not lengths:
                self._error(411, "Content-Length is required.")
                return
        elif length:
            self._error(400, "Read requests must not contain a body.")
            return
        try:
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                self._error(400, "Incomplete request body.")
                return
        except (OSError, TimeoutError):
            self._error(408, "Request body timed out.")
            return
        connection_tokens = set()
        for value in self.headers.get_all("Connection", []):
            connection_tokens.update(token.strip().lower() for token in value.split(","))
        excluded = HOP_HEADERS | connection_tokens | {
            "host", "origin", "content-length", "cookie", "authorization",
        }
        headers = {key: value for key, value in self.headers.items() if key.lower() not in excluded}
        upstream_authority = "127.0.0.1:%s" % self.server.upstream_port
        headers["Host"] = upstream_authority
        if origins:
            headers["Origin"] = "http://" + upstream_authority
        if body is not None:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port,
                                                timeout=UPSTREAM_TIMEOUT)
        started = False
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            response_lengths = response.headers.get_all("Content-Length", [])
            if len(response_lengths) > 1 or (response_lengths and (len(response_lengths[0]) > 10 or not re.fullmatch(r"[0-9]+", response_lengths[0]))):
                raise ValueError("Invalid upstream length")
            if response_lengths and int(response_lengths[0]) > MAX_RESPONSE:
                raise ValueError("Upstream response too large")
            response_tokens = set()
            for value in response.headers.get_all("Connection", []):
                response_tokens.update(token.strip().lower() for token in value.split(","))
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS | response_tokens | {"server", "date"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            self.close_connection = True
            if self.command != "HEAD":
                remaining = MAX_RESPONSE
                while remaining:
                    chunk = response.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (OSError, http.client.HTTPException, ValueError):
            if not started:
                self._error(502, "The session hub IDE forward is unavailable. Start the hub in your workspace and forward its configured port to this Mac's loopback address.")
            self.close_connection = True
        finally:
            connection.close()

    do_GET = _proxy
    do_HEAD = _proxy
    do_POST = _proxy

    def do_CONNECT(self):
        self._error(405, "This gateway does not support tunnels.")

    def do_OPTIONS(self):
        self._error(405, "Cross-origin access is not supported.")

    def do_PUT(self):
        self._error(405, "This method is not supported.")

    do_DELETE = do_PUT
    do_PATCH = do_PUT
    do_TRACE = do_PUT

    def handle_expect_100(self):
        self._error(400, "Expect is not supported.")
        return False


def run_gateway(port=8787, upstream_port=4317, bind="auto"):
    if not 1 <= port <= 65535 or not 1 <= upstream_port <= 65535:
        raise ValueError("Ports must be between 1 and 65535")
    if bind != "auto":
        address = _address(bind, allow_loopback=True)
        if not address:
            raise ValueError("Bind must be a private LAN IPv4 address, or loopback for local verification")
        server = GatewayServer((address, port), upstream_port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return
    if platform.system() != "Darwin":
        raise ValueError("Automatic LAN detection requires macOS; use --bind with an explicit private IPv4 address")
    server = None
    thread = None
    current = None
    waiting_reported = False
    try:
        while True:
            address = detect_lan_address()
            if address != current:
                if server:
                    server.shutdown()
                    server.server_close()
                    thread.join()
                    server = None
                current = None
            if address and server is None:
                try:
                    server = GatewayServer((address, port), upstream_port)
                except OSError:
                    print("Gateway is waiting for the selected LAN address to become available.", file=sys.stderr)
                else:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    current = address
                    waiting_reported = False
                    print("Session hub LAN gateway: http://%s:%s" % (address, port), file=sys.stderr)
            elif not address and not waiting_reported:
                print("Gateway is waiting for an active private Wi-Fi or Ethernet IPv4 address.", file=sys.stderr)
                waiting_reported = True
            time.sleep(3)
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join()
