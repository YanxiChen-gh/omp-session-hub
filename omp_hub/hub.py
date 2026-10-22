"""Loopback session discovery and workspace creation server."""

import importlib.resources
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

from .http import Handler, RequestError, Server

HOST_ID = re.compile(r"^[a-z0-9-]{8,64}$")
MAX_OUTPUT = 2 * 1024 * 1024
HOST_FIELDS = ("instanceId", "generation", "pid", "sessionId", "sessionName", "cwd", "model", "startedAt", "participants", "relayConnected", "inputRequired", "busy", "access")


def command_json(argv, data=None, timeout=15, allow_error=False):
    """Bound process runtime and captured output without exposing stderr."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                   stdout=output, stderr=errors)
        try:
            if data is not None:
                process.stdin.write(json.dumps(data).encode("utf-8"))
                process.stdin.close()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise RequestError(504, "Operation timed out; creation may still be running. Inspect Herdr before retrying.")
                if output.tell() > MAX_OUTPUT or errors.tell() > MAX_OUTPUT:
                    raise RequestError(502, "Command output exceeded its limit")
                time.sleep(0.05)
            output.seek(0)
            raw = output.read(MAX_OUTPUT + 1)
            if len(raw) > MAX_OUTPUT:
                raise RequestError(502, "Command output exceeded its limit")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise RequestError(502, "Local command returned invalid JSON")
            if not isinstance(value, dict):
                raise RequestError(502, "Local command returned an invalid response")
            if process.returncode and not (allow_error and isinstance(value.get("error"), str)):
                raise RequestError(502, "Local command failed. Check OMP, Herdr, and the workspace adapter configuration.")
            return value
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def creation_spec(body):
    allowed = {"repository", "checkout", "agent", "editor", "prompt", "workspaceId", "cloneRepository", "cloneRoot"}
    if body.keys() - allowed:
        raise RequestError(400, "Unknown creation option")
    result = dict(body)
    for key in ("repository", "prompt", "workspaceId", "cloneRepository", "cloneRoot"):
        if key in result and (not isinstance(result[key], str) or "\x00" in result[key] or len(result[key]) > (24000 if key == "prompt" else 4096)):
            raise RequestError(400, "Invalid " + key)
    for key in ("agent", "editor"):
        if key in result and type(result[key]) is not bool:
            raise RequestError(400, "Invalid " + key)
    result.setdefault("agent", True)
    result.setdefault("editor", False)
    result.setdefault("prompt", "")
    result.setdefault("checkout", "fresh")
    if result["checkout"] not in ("fresh", "primary"):
        raise RequestError(400, "Choose a fresh or primary checkout")
    if result.get("workspaceId"):
        if result["workspaceId"].startswith("-") or len(result["workspaceId"]) > 256:
            raise RequestError(400, "Invalid workspace")
        if result.get("repository") or result.get("cloneRepository") or result.get("cloneRoot"):
            raise RequestError(400, "A new tab uses its existing workspace repository")
    elif result.get("cloneRepository"):
        if result.get("repository"):
            raise RequestError(400, "Choose either a repository or a GitHub clone")
    elif not result.get("repository"):
        raise RequestError(400, "Choose a repository")
    if not result["agent"] and result["prompt"]:
        raise RequestError(400, "A shell workspace cannot accept an agent prompt")
    return result


class Hub:
    def __init__(self, adapter):
        self.adapter = str(Path(adapter).expanduser().resolve(strict=True))
        if not Path(self.adapter).is_file():
            raise ValueError("Workspace adapter must be a file")
        self.lock = threading.Lock()
        self.discovery_lock = threading.Lock()
        self.jobs = OrderedDict()
        self.cache = None
        self.cache_at = 0

    def inventory(self):
        result = command_json([sys.executable, self.adapter, "inventory"], timeout=30)
        if not isinstance(result.get("repositories"), list) or not isinstance(result.get("workspaces"), list):
            raise RequestError(502, "Adapter returned an invalid inventory")
        return result

    def sessions(self, fresh=False):
        with self.discovery_lock:
            if not fresh and self.cache is not None and time.monotonic() - self.cache_at < 1:
                return self.cache
            result = command_json(["omp", "collab", "list", "--json"])
            if result.get("version") != 1 or not isinstance(result.get("hosts"), list):
                raise RequestError(502, "Unsupported OMP collaboration registry")
            hosts = []
            for item in result["hosts"]:
                if not isinstance(item, dict) or not isinstance(item.get("instanceId"), str) or not HOST_ID.fullmatch(item["instanceId"]):
                    raise RequestError(502, "OMP returned an invalid host")
                host = {key: item.get(key) for key in HOST_FIELDS}
                hosts.append(host)
            self.cache = {"sessions": hosts}
            self.cache_at = time.monotonic()
            return self.cache

    def open_session(self, instance_id, body):
        if not HOST_ID.fullmatch(instance_id) or set(body) != {"generation"} or type(body["generation"]) is not int:
            raise RequestError(400, "Invalid session selection")
        hosts = self.sessions(fresh=True)["sessions"]
        host = next((item for item in hosts if item["instanceId"] == instance_id), None)
        if host is None:
            raise RequestError(404, "Session is no longer available. Refresh the list.")
        if host["generation"] != body["generation"]:
            raise RequestError(409, "Session changed. Refresh the list before opening it.")
        argv = ["omp", "collab", "link", instance_id, "--json"]
        if host["access"] == "view":
            argv.append("--view")
        link = command_json(argv)
        if link.get("instanceId") != instance_id or link.get("generation") != body["generation"]:
            raise RequestError(409, "Session changed. Refresh the list before opening it.")
        url = link.get("url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid = parsed and parsed.hostname and not parsed.username and not parsed.password and (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")))
        except ValueError:
            valid = False
        if not valid:
            raise RequestError(502, "OMP returned an unsafe browser URL")
        return {"url": url}

    def create(self, body):
        spec = creation_spec(body)
        with self.lock:
            if sum(job["status"] == "preparing" for job in self.jobs.values()) >= 4:
                raise RequestError(429, "Four workspaces are already preparing. Wait for one to finish.")
            while len(self.jobs) >= 64:
                old = next((key for key, value in self.jobs.items() if value["status"] != "preparing"), None)
                if old is None:
                    raise RequestError(429, "Too many pending jobs")
                self.jobs.pop(old)
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {"status": "preparing"}
        threading.Thread(target=self._create, args=(job_id, spec), daemon=True).start()
        return {"jobId": job_id}

    def _create(self, job_id, spec):
        try:
            result = command_json([sys.executable, self.adapter, "create"], spec, timeout=180, allow_error=True)
            if result.get("error"):
                message = result["error"]
                raise RequestError(422, message[:1000] if isinstance(message, str) else "Workspace creation failed")
            if not isinstance(result.get("workspaceId"), str) or not isinstance(result.get("paneId"), str):
                raise RequestError(502, "Adapter did not report a ready workspace")
            state = {"status": "ready", "result": result}
        except RequestError as exc:
            state = {"status": "failed", "error": exc.message}
        except OSError:
            state = {"status": "failed", "error": "Could not execute the workspace adapter"}
        except Exception:
            state = {"status": "failed", "error": "Workspace creation failed unexpectedly. Inspect Herdr before retrying."}
        with self.lock:
            self.jobs[job_id] = state

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise RequestError(404, "Creation status expired or the hub restarted. Inspect Herdr before retrying.")
            return dict(self.jobs[job_id])


def make_handler(hub):
    class HubHandler(Handler):
        def dispatch(self):
            try:
                path = self.boundary()
                if self.command in ("GET", "HEAD"):
                    if path == "/healthz":
                        self.send_json(200, {"ok": True})
                    elif path == "/api/sessions":
                        self.send_json(200, hub.sessions())
                    elif path == "/api/inventory":
                        self.send_json(200, hub.inventory())
                    elif path.startswith("/api/jobs/"):
                        self.send_json(200, hub.job(path[len("/api/jobs/"):]))
                    else:
                        names = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
                        if path not in names:
                            raise RequestError(404, "Not found")
                        name, content_type = names[path]
                        data = importlib.resources.files("omp_hub").joinpath("static").joinpath(name).read_bytes()
                        self.send_data(200, data, content_type)
                elif self.command == "POST":
                    body = self.json_body()
                    if path == "/api/workspaces":
                        self.send_json(202, hub.create(body))
                    elif path.startswith("/api/sessions/") and path.endswith("/open"):
                        self.send_json(200, hub.open_session(path[len("/api/sessions/"):-len("/open")], body))
                    else:
                        raise RequestError(404, "Not found")
                else:
                    raise RequestError(405, "Method not allowed")
            except RequestError as exc:
                self.send_json(exc.status, {"error": exc.message})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self.send_json(503, {"error": "Local service unavailable. Check OMP and the workspace adapter."})
        do_GET = dispatch
        do_HEAD = dispatch
        do_POST = dispatch
    return HubHandler


def run_hub(port, adapter):
    hub = Hub(adapter)
    with Server(("127.0.0.1", port), make_handler(hub)) as server:
        print("OMP session hub: http://127.0.0.1:%s" % server.server_address[1], flush=True)
        server.serve_forever()
