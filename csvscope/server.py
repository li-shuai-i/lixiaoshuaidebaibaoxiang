"""Local HTTP server for csvscope.

A thin JSON layer over :mod:`csvscope.core` plus static file serving.  Only
``http.server`` is used, so the whole app installs with zero dependencies.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import DataError, Dataset, __version__

STATIC_DIR = Path(__file__).parent / "static"
EXAMPLE_PATH = Path(__file__).parent.parent / "examples" / "orders.csv"

#: Refuse request bodies larger than this (bytes).
MAX_BODY = 64 * 1024 * 1024
#: How many datasets to keep in memory at once.
MAX_DATASETS = 8

_registry: dict[str, Dataset] = {}
_registry_lock = threading.Lock()


def _store(dataset: Dataset) -> str:
    key = secrets.token_hex(8)
    with _registry_lock:
        _registry[key] = dataset
        while len(_registry) > MAX_DATASETS:
            _registry.pop(next(iter(_registry)))
    return key


def _lookup(key: str) -> Dataset:
    with _registry_lock:
        dataset = _registry.get(key)
    if dataset is None:
        raise DataError("Dataset expired or unknown. Please load the file again.")
    return dataset


class Handler(BaseHTTPRequestHandler):
    server_version = f"csvscope/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send_bytes(self, payload: bytes, content_type: str,
                    status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_json(self, data: Any,
                   status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _fail(self, message: str,
              status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise DataError("File too large for the local server (limit 64 MB).")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataError("Request body must be UTF-8 JSON.") from exc
        if not isinstance(payload, dict):
            raise DataError("Request body must be a JSON object.")
        return payload

    # -- routes ------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/assets/"):
                return self._serve_static(path[len("/assets/"):])
            if path == "/api/health":
                return self._send_json(
                    {"ok": True, "version": __version__, "datasets": len(_registry)}
                )
            if path == "/api/example":
                if not EXAMPLE_PATH.exists():
                    return self._fail("Example dataset is not bundled.", HTTPStatus.NOT_FOUND)
                text = EXAMPLE_PATH.read_text(encoding="utf-8")
                return self._send_json({"name": EXAMPLE_PATH.name, "content": text})
            if path == "/favicon.ico":
                return self._send_bytes(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
            return self._fail("Not found", HTTPStatus.NOT_FOUND)
        except DataError as exc:
            self._fail(str(exc))

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        routes = {
            "/api/load": self._load,
            "/api/query": self._query,
            "/api/aggregate": self._aggregate,
            "/api/histogram": self._histogram,
        }
        handler = routes.get(path)
        if handler is None:
            return self._fail("Not found", HTTPStatus.NOT_FOUND)
        try:
            handler(self._read_json())
        except DataError as exc:
            self._fail(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(f"Unexpected server error: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    # -- handlers ---------------------------------------------------------- #

    def _load(self, payload: dict) -> None:
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DataError("No file content received.")
        name = str(payload.get("name") or "dataset.csv")
        dataset = Dataset.from_text(content, name=name)
        key = _store(dataset)
        response = dataset.summary()
        response.update(
            {
                "id": key,
                "profiles": dataset.profiles(),
                "preview": dataset.rows(limit=50),
            }
        )
        self._send_json(response)

    def _query(self, payload: dict) -> None:
        dataset = _lookup(payload.get("id", ""))
        result = dataset.query(
            filters=payload.get("filters"),
            sort=payload.get("sort"),
            page=payload.get("page", 1),
            page_size=payload.get("page_size", 50),
        )
        self._send_json(result)

    def _aggregate(self, payload: dict) -> None:
        dataset = _lookup(payload.get("id", ""))
        result = dataset.aggregate(
            group_by=payload.get("group_by", ""),
            metric=payload.get("metric"),
            agg=payload.get("agg", "count"),
            limit=payload.get("limit", 20),
            filters=payload.get("filters"),
        )
        self._send_json(result)

    def _histogram(self, payload: dict) -> None:
        dataset = _lookup(payload.get("id", ""))
        result = dataset.histogram(
            ref=payload.get("column", ""),
            bins=payload.get("bins", 15),
            filters=payload.get("filters"),
        )
        self._send_json(result)

    # -- static ------------------------------------------------------------ #

    def _serve_static(self, relative: str) -> None:
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self._fail("Forbidden", HTTPStatus.FORBIDDEN)
        if not target.is_file():
            return self._fail("Not found", HTTPStatus.NOT_FOUND)

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self._send_bytes(target.read_bytes(), content_type)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], verbose: bool = False) -> None:
        super().__init__(address, Handler)
        self.verbose = verbose


def serve(host: str = "127.0.0.1", port: int = 8787, verbose: bool = False) -> Server:
    """Create (but do not start) the HTTP server."""
    return Server((host, port), verbose=verbose)
