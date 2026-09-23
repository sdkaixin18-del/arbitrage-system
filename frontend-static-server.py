#!/usr/bin/env python3
from __future__ import annotations

import argparse
import mimetypes
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FrontendHandler(BaseHTTPRequestHandler):
    dist_dir: Path
    backend_url: str

    def do_GET(self) -> None:
        if self.path.startswith("/api/"):
            self.proxy_api()
            return
        self.serve_static()

    def do_HEAD(self) -> None:
        if self.path.startswith("/api/"):
            self.proxy_api(head_only=True)
            return
        self.serve_static(head_only=True)

    def do_POST(self) -> None:
        self.proxy_api()

    def do_PUT(self) -> None:
        self.proxy_api()

    def do_PATCH(self) -> None:
        self.proxy_api()

    def do_DELETE(self) -> None:
        self.proxy_api()

    def proxy_api(self, head_only: bool = False) -> None:
        body = None
        length = int(self.headers.get("Content-Length") or "0")
        if length:
            body = self.rfile.read(length)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "content-length"}
        }
        request = Request(
            f"{self.backend_url}{self.path}",
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            with urlopen(request, timeout=60) as response:
                payload = b"" if head_only else response.read()
                self.send_response(response.status)
                self.copy_response_headers(response.headers, len(payload))
                self.end_headers()
                if not head_only:
                    self.write_payload(payload)
        except HTTPError as exc:
            payload = b"" if head_only else exc.read()
            self.send_response(exc.code)
            self.copy_response_headers(exc.headers, len(payload))
            self.end_headers()
            if not head_only:
                self.write_payload(payload)
        except URLError as exc:
            payload = f"Backend unavailable: {exc.reason}".encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if not head_only:
                self.write_payload(payload)

    def copy_response_headers(self, headers, payload_length: int) -> None:
        skipped = {"connection", "transfer-encoding", "content-length"}
        for key, value in headers.items():
            if key.lower() not in skipped:
                self.send_header(key, value)
        self.send_header("Content-Length", str(payload_length))

    def serve_static(self, head_only: bool = False) -> None:
        raw_path = self.path.split("?", 1)[0].split("#", 1)[0]
        relative = raw_path.lstrip("/") or "index.html"
        candidate = (self.dist_dir / relative).resolve()
        if not str(candidate).startswith(str(self.dist_dir.resolve())):
            self.send_error(403)
            return
        if not candidate.is_file():
            if "." in Path(relative).name:
                self.send_error(404)
                return
            candidate = self.dist_dir / "index.html"
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        stat = candidate.stat()
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable")
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(stat.st_size))
        self.end_headers()
        if not head_only:
            try:
                with candidate.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=256 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                return

    def write_payload(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--dist", default=str(Path(__file__).resolve().parent / "frontend" / "dist"))
    args = parser.parse_args()

    dist_dir = Path(args.dist).resolve()
    if not (dist_dir / "index.html").is_file():
        raise SystemExit(f"frontend dist not found: {dist_dir}")
    FrontendHandler.dist_dir = dist_dir
    FrontendHandler.backend_url = args.backend.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), FrontendHandler)
    print(f"Frontend static: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
