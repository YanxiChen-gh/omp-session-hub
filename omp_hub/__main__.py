"""Command-line entry point."""

import argparse
import sys

from . import __version__


def main():
    parser = argparse.ArgumentParser(prog="omp-session-hub")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    hub = commands.add_parser("hub", help="Serve the CDE dashboard on loopback")
    hub.add_argument("--port", type=int, default=4317)
    hub.add_argument("--adapter", required=True)
    gateway = commands.add_parser("gateway", help="Expose an IDE-forwarded dashboard on the LAN")
    gateway.add_argument("--port", type=int, default=8787)
    gateway.add_argument("--upstream-port", type=int, default=4317)
    gateway.add_argument("--bind", default="auto")
    commands.add_parser("service", help="Install and manage background services")
    if len(sys.argv) > 1 and sys.argv[1] == "service":
        from .services import service_main
        return service_main(sys.argv[2:])
    args = parser.parse_args()
    for name in ("port", "upstream_port"):
        value = getattr(args, name, 1)
        if not 1 <= value <= 65535:
            parser.error("Ports must be between 1 and 65535")
    try:
        if args.command == "hub":
            from .hub import run_hub
            run_hub(args.port, args.adapter)
        else:
            from .gateway import run_gateway
            run_gateway(args.port, args.upstream_port, args.bind)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
    except KeyboardInterrupt:
        return 0
    return 0


def entrypoint():
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
