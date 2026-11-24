"""Command line entry point: ``python -m csvscope`` or ``csvscope``."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

from .core import __version__
from .server import serve


def _free_port(host: str, preferred: int) -> int:
    """Return ``preferred`` if it is free, otherwise any free port."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    return preferred


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csvscope",
        description="Local-first CSV/TSV data explorer. Opens in your browser, "
        "keeps your data on your machine.",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("-p", "--port", type=int, default=8787,
                        help="preferred port (default: 8787, falls back to any free port)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window automatically")
    parser.add_argument("--verbose", action="store_true",
                        help="log every HTTP request")
    parser.add_argument("-V", "--version", action="version",
                        version=f"csvscope {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    port = _free_port(args.host, args.port)
    server = serve(args.host, port, verbose=args.verbose)
    url = f"http://{args.host}:{port}/"

    banner = [
        "",
        f"  csvscope {__version__}  —  local-first CSV explorer",
        f"  running at  {url}",
        "  press Ctrl+C to stop",
        "",
    ]
    print("\n".join(banner), flush=True)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.\n")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
