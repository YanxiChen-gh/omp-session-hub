"""Per-user launchd services and a portable, identity-safe supervisor."""

import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import platform
import plistlib
import select
import signal
import socket
import stat
import subprocess
import sys
import time

LABEL_PREFIX = "io.github.omp-session-hub."
LOG_LIMIT = 1024 * 1024


def _private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Service state directory must be owned by you and have mode 0700: %s" % path)
    return path


def _state_dir():
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    if not base.is_absolute():
        raise ValueError("XDG_STATE_HOME must be an absolute path")
    return _private_directory(base / "omp-session-hub")


def _read_private(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Service files must be private regular files owned by you")
        return json.load(stream)


def _write_private(path, value):
    temporary = path.with_name(path.name + ".new-%s" % os.getpid())
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.write("\n")
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _execution():
    python = str(Path(getattr(sys, "_base_executable", None) or sys.executable).resolve())
    search_paths = [
        str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin",
        "/usr/local/bin", "/usr/bin", "/bin",
    ] + os.environ.get("PATH", "").split(os.pathsep)
    environment = {"PATH": os.pathsep.join(dict.fromkeys(path for path in search_paths if path))}
    invoked = Path(sys.argv[0]).expanduser()
    resolved = invoked.resolve()
    if resolved.suffix == ".pyz":
        return [python, str(resolved)], environment
    root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = root
    return [python, "-m", "omp_hub"], environment


def _control(directory, name, command):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1)
    try:
        client.connect(str(directory / (name + ".sock")))
        client.sendall((command + "\n").encode("ascii"))
        data = bytearray()
        while len(data) < 4096:
            chunk = client.recv(4096 - len(data))
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in data:
                break
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        client.close()


def _lock(directory, name):
    descriptor = os.open(str(directory / (name + ".lock")),
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(descriptor)
        raise ValueError("Unsafe service lock file")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def _rotate_log(directory, name):
    path = directory / (name + ".log")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Unsafe service log file")
        if info.st_size >= LOG_LIMIT:
            os.replace(str(path), str(path.with_suffix(".log.1")))
    return os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)


def _parent_death_guard():
    if not sys.platform.startswith("linux"):
        return None
    # Load libc before fork: this supervisor has no threads, and the child only
    # registers its death signal and checks the fork-to-prctl parent-death race.
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                      ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    parent = os.getpid()

    def guard():
        if prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            os._exit(1)

    return guard


def _supervise(directory, name):
    lock = _lock(directory, name)
    if lock is None:
        return 0
    listener = None
    child = None
    stopping = False
    socket_path = directory / (name + ".sock")
    old_handlers = {}

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    try:
        config = _read_private(directory / (name + ".json"))
        child_guard = _parent_death_guard()
        socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        os.chmod(str(socket_path), 0o600)
        listener.listen(8)
        listener.setblocking(False)
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, stop)
        next_start = 0.0
        failures = 0
        started_at = 0.0
        log_descriptor = None
        log_size = 0
        try:
            log_descriptor = _rotate_log(directory, name)
            log_size = os.fstat(log_descriptor).st_size
            while not stopping:
                now = time.monotonic()
                if child is not None and child.poll() is not None:
                    if child.stdout:
                        child.stdout.close()
                    child = None
                    failures = 0 if now - started_at > 30 else failures + 1
                    next_start = now + min(10, max(1, failures))
                if child is None and now >= next_start:
                    environment = os.environ.copy()
                    environment.update(config["environment"])
                    try:
                        child = subprocess.Popen(config["command"], stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 env=environment, preexec_fn=child_guard,
                                                 close_fds=True, umask=0o077)
                        os.set_blocking(child.stdout.fileno(), False)
                        started_at = now
                    except OSError:
                        failures += 1
                        next_start = now + min(10, failures)
                readers = [listener]
                if child is not None and child.stdout:
                    readers.append(child.stdout)
                ready, _, _ = select.select(readers, [], [], 0.25)
                if listener in ready:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(0.5)
                        try:
                            command = connection.recv(64).decode("ascii").strip()
                            if command == "stop":
                                stopping = True
                            response = {"service": name, "running": not stopping,
                                        "childRunning": child is not None and child.poll() is None}
                            connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
                        except (OSError, UnicodeError):
                            pass
                if child is not None and child.stdout in ready:
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if chunk:
                        if log_size + len(chunk) > LOG_LIMIT:
                            os.close(log_descriptor)
                            log_descriptor = None
                            log_path = directory / (name + ".log")
                            os.replace(str(log_path), str(log_path.with_suffix(".log.1")))
                            log_descriptor = _rotate_log(directory, name)
                            log_size = 0
                        os.write(log_descriptor, chunk)
                        log_size += len(chunk)
        finally:
            if log_descriptor is not None:
                os.close(log_descriptor)
    finally:
        if child is not None:
            # Popen retains the exact child identity. Never signal a PID from disk.
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if child.stdout:
                child.stdout.close()
        if listener is not None:
            listener.close()
            socket_path.unlink(missing_ok=True)
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)
        os.close(lock)
    return 0


def _launchctl(args, check=True):
    result = subprocess.run(["/bin/launchctl"] + args, capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise ValueError("launchctl could not manage the per-user service; run this command from your logged-in Mac desktop session")
    return result


def _agent_path(name):
    directory = Path.home() / "Library" / "LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (LABEL_PREFIX + name + ".plist")


def _managed_agent(path, name):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("Refusing to replace or stop an unsafe LaunchAgent")
    with path.open("rb") as stream:
        value = plistlib.load(stream)
    args = value.get("ProgramArguments", [])
    if value.get("Label") != LABEL_PREFIX + name or args[-3:] != ["service", "_supervise", name]:
        raise ValueError("Refusing to replace or stop an unmanaged LaunchAgent")
    return value


def _install_agent(name):
    path = _agent_path(name)
    label = LABEL_PREFIX + name
    runtime, environment = _execution()
    value = {
        "Label": label,
        "ProgramArguments": runtime + ["service", "_supervise", name],
        "EnvironmentVariables": dict(environment, XDG_STATE_HOME=str(_state_dir().parent)),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "Umask": 0o077,
    }
    if path.exists() or path.is_symlink():
        previous = _managed_agent(path, name)
        if previous == value:
            return path
        _launchctl(["bootout", "gui/%s/%s" % (os.getuid(), label)], check=False)
    temporary = path.with_name(path.name + ".new-%s" % os.getpid())
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            plistlib.dump(value, stream)
        os.replace(str(temporary), str(path))
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _ensure(directory, name):
    if _control(directory, name, "status"):
        return
    if platform.system() == "Darwin":
        path = _install_agent(name)
        label = "gui/%s/%s" % (os.getuid(), LABEL_PREFIX + name)
        if _launchctl(["print", label], check=False).returncode:
            _launchctl(["bootstrap", "gui/%s" % os.getuid(), str(path)])
        _launchctl(["kickstart", label])
    else:
        runtime, environment = _execution()
        child_environment = os.environ.copy()
        child_environment.update(environment)
        subprocess.Popen(runtime + ["service", "_supervise", name],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=child_environment,
                         start_new_session=True, close_fds=True, umask=0o077)
        # The supervisor lock arbitrates concurrent ensure calls.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _control(directory, name, "status"):
            return
        time.sleep(0.1)
    raise ValueError("Service did not start; inspect the private service state and logs at %s" % directory)


def _stop(directory, name):
    if platform.system() == "Darwin":
        path = _agent_path(name)
        if path.exists() or path.is_symlink():
            _managed_agent(path, name)
            label = "gui/%s/%s" % (os.getuid(), LABEL_PREFIX + name)
            if _launchctl(["print", label], check=False).returncode == 0:
                _launchctl(["bootout", label])
    _control(directory, name, "stop")
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        lock = _lock(directory, name)
        if lock is not None:
            os.close(lock)
            return
        time.sleep(0.1)
    raise ValueError("Service has not stopped; no unverified process was signaled")


def _configuration(options, previous):
    saved = previous.get("options", {}) if previous else {}
    port = options.port if options.port is not None else saved.get("port", 4317 if options.name == "hub" else 8787)
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    settings = {"port": port}
    arguments = [options.name, "--port", str(port)]
    if options.name == "hub":
        adapter = options.adapter if options.adapter is not None else saved.get("adapter")
        if not adapter:
            raise ValueError("The hub service requires --adapter with an absolute executable path")
        path = Path(adapter).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ValueError("The adapter must be an existing absolute file path")
        settings["adapter"] = str(path.resolve())
        arguments.extend(["--adapter", settings["adapter"]])
        if options.upstream_port is not None:
            raise ValueError("--upstream-port applies only to gateway")
    else:
        if options.adapter is not None:
            raise ValueError("--adapter applies only to hub")
        upstream = options.upstream_port if options.upstream_port is not None else saved.get("upstream_port", 4317)
        if not 1 <= upstream <= 65535:
            raise ValueError("Upstream port must be between 1 and 65535")
        settings["upstream_port"] = upstream
        arguments.extend(["--upstream-port", str(upstream), "--bind", "auto"])
    if previous and options.action in ("ensure", "start") and settings == saved:
        return previous
    runtime, environment = _execution()
    return {"command": runtime + arguments, "environment": environment, "options": settings}


def service_main(args):
    parser = argparse.ArgumentParser(prog="omp-session-hub service")
    parser.add_argument("action", choices=("install", "ensure", "start", "status", "stop", "_supervise"))
    parser.add_argument("name", choices=("hub", "gateway"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--adapter")
    parser.add_argument("--upstream-port", type=int)
    options = parser.parse_args(args)
    try:
        directory = _state_dir()
        if options.action == "_supervise":
            return _supervise(directory, options.name)
        if options.action == "status":
            status = _control(directory, options.name, "status")
            print(json.dumps(status or {"service": options.name, "running": False, "childRunning": False}))
            return 0 if status and status.get("running") else 1
        if options.action == "stop":
            _stop(directory, options.name)
            print("%s service stopped; configuration and private logs preserved" % options.name)
            return 0
        path = directory / (options.name + ".json")
        previous = _read_private(path) if path.exists() or path.is_symlink() else None
        config = _configuration(options, previous)
        if config != previous:
            _stop(directory, options.name)
            _write_private(path, config)
        _ensure(directory, options.name)
        print("%s service running" % options.name)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print("Service error: %s" % error, file=sys.stderr)
        return 1
