#!/usr/bin/env python3
"""Local read-only Pulse relay over one dedicated persistent SSH process."""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import select
import signal
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


PULSE_URLS = (
    "https://pulse-lite-api.astro-btc.xyz/api/query/new",
    "https://pulse-api.astro-btc.xyz/api/query/second",
)

REMOTE_SOURCE = r'''
import base64, concurrent.futures, gzip, json, sys, time, urllib.request
URLS = (
    "https://pulse-lite-api.astro-btc.xyz/api/query/new",
    "https://pulse-api.astro-btc.xyz/api/query/second",
)
def fetch(url, timeout):
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent":"pulse-ssh-bridge/1","Cache-Control":"no-cache"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        payload = json.loads(body)
        if response.status != 200 or not isinstance(payload, dict) or payload.get("code") != 0:
            raise RuntimeError("invalid Pulse response")
    return payload, {"url":url,"durationMs":round((time.monotonic()-started)*1000,1),"bytes":len(body)}
for raw in sys.stdin.buffer:
    try:
        command = json.loads(raw)
        request_id = command["id"]
        timeout = max(1.0, min(8.0, float(command.get("timeoutSeconds", 5.0))))
        payloads, sources, failures = [], [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(fetch, url, timeout): url for url in URLS}
            for future in concurrent.futures.as_completed(futures):
                try:
                    payload, source = future.result(); payloads.append(payload); sources.append(source)
                except Exception as error:
                    failures.append({"url":futures[future],"error":type(error).__name__+": "+str(error)})
        if not payloads:
            raise RuntimeError("; ".join(item["error"] for item in failures) or "no Pulse payloads")
        packed = gzip.compress(json.dumps(payloads,separators=(",",":"),ensure_ascii=False).encode(), compresslevel=1)
        result = {"payloadEncoding":"gzip+base64","payloadsGzipBase64":base64.b64encode(packed).decode(),
                  "sourceCount":len(URLS),"successCount":len(payloads),"failures":failures,"sources":sources,
                  "fetchedAtMs":int(time.time()*1000),"compressedBytes":len(packed)}
        output = {"id":request_id,"ok":True,"result":result}
    except Exception as error:
        output = {"id":locals().get("request_id"),"ok":False,"error":type(error).__name__+": "+str(error)}
    sys.stdout.write(json.dumps(output,separators=(",",":"),ensure_ascii=False)+"\n"); sys.stdout.flush()
'''


class PersistentPulseSSH:
    def __init__(self, target: str, key: Path) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+", target):
            raise ValueError("invalid Pulse SSH target")
        if not key.is_file():
            raise FileNotFoundError(f"Pulse SSH key does not exist: {key}")
        if key.stat().st_mode & 0o077:
            raise PermissionError("Pulse SSH key must not be group/other-readable")
        self.target = target
        self.key = key
        self.process: subprocess.Popen[bytes] | None = None
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.last_error: str | None = None
        self.last_success_at_ms: int | None = None
        self.last_rtt_ms: float | None = None

    def _command(self) -> list[str]:
        encoded = base64.b64encode(REMOTE_SOURCE.encode()).decode()
        remote = f"python3 -u -c 'import base64;exec(compile(base64.b64decode(\"{encoded}\"),\"pulse_ssh_bridge\",\"exec\"))'"
        return [
            "/usr/bin/ssh", "-T", "-i", str(self.key), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
            self.target, remote,
        ]

    def connect(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        self.process = subprocess.Popen(
            self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self.buffer.clear()

    def _readline(self, timeout: float) -> bytes:
        assert self.process is not None and self.process.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.buffer[:newline]); del self.buffer[:newline + 1]; return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pulse SSH response timed out")
            ready, _, _ = select.select([self.process.stdout.fileno()], [], [], remaining)
            if not ready:
                raise TimeoutError("Pulse SSH response timed out")
            chunk = os.read(self.process.stdout.fileno(), 65_536)
            if not chunk:
                detail = ""
                if self.process.stderr is not None:
                    try: detail = self.process.stderr.read(2048).decode(errors="replace").strip()
                    except OSError: pass
                suffix = f": {detail}" if detail else ""
                raise ConnectionError(f"Pulse SSH stream closed (exit={self.process.poll()}){suffix}")
            self.buffer.extend(chunk)

    def fetch(self, timeout: float) -> dict:
        with self.lock:
            started = time.perf_counter()
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    self.connect()
                    assert self.process is not None and self.process.stdin is not None
                    request_id = os.urandom(12).hex()
                    self.process.stdin.write(json.dumps({"id":request_id,"timeoutSeconds":timeout}).encode()+b"\n")
                    self.process.stdin.flush()
                    while True:
                        response = json.loads(self._readline(timeout + 0.75))
                        if response.get("id") == request_id:
                            break
                    if not response.get("ok"):
                        raise RuntimeError(str(response.get("error") or "Pulse SSH request failed"))
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise ValueError("Pulse SSH returned invalid result")
                    self.last_rtt_ms = round((time.perf_counter()-started)*1000, 1)
                    self.last_success_at_ms = int(time.time()*1000)
                    self.last_error = None
                    return {**result, "sshRttMs": self.last_rtt_ms}
                except Exception as error:
                    last_error = error
                    self.close()
                    if attempt == 0:
                        continue
            assert last_error is not None
            self.last_error = f"{type(last_error).__name__}: {last_error}"
            raise last_error

    def status(self) -> dict:
        connected = self.process is not None and self.process.poll() is None
        return {"status":"ok" if connected else "warming", "service":"pulse-ssh-bridge",
                "connected":connected, "readOnly":True, "containerUsed":False,
                "lastSuccessAtMs":self.last_success_at_ms,"lastRttMs":self.last_rtt_ms,
                "lastError":self.last_error}

    def close(self) -> None:
        process, self.process = self.process, None
        self.buffer.clear()
        if process is not None and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=1)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    bridge: PersistentPulseSSH


class Handler(BaseHTTPRequestHandler):
    server: Server
    def log_message(self, *_args) -> None: return
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/health", "/pulse"}:
            self.send_error(HTTPStatus.NOT_FOUND); return
        if parsed.path == "/health":
            body, status = self.server.bridge.status(), HTTPStatus.OK
        else:
            query = parse_qs(parsed.query)
            try: timeout = max(1.5, min(8.0, float(query.get("timeoutSeconds", ["5"])[0])))
            except ValueError: timeout = 5.0
            try: body, status = self.server.bridge.fetch(timeout), HTTPStatus.OK
            except Exception as error:
                body, status = {**self.server.bridge.status(),"error":f"{type(error).__name__}: {error}"}, HTTPStatus.SERVICE_UNAVAILABLE
        payload = json.dumps(body,separators=(",",":"),ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(payload))); self.send_header("Cache-Control","no-store")
        self.end_headers(); self.wfile.write(payload)


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=int(os.environ.get("ASTRO_PULSE_SSH_PORT","8766")))
    args=parser.parse_args()
    target=os.environ.get("ASTRO_PULSE_SSH_TARGET","ubuntu@192.0.2.10")
    key=Path(os.environ.get("ASTRO_PULSE_SSH_KEY","/home/example/Downloads/astro.pem")).expanduser()
    bridge=PersistentPulseSSH(target,key); bridge.connect()
    server=Server((args.host,args.port),Handler); server.bridge=bridge
    stopping=threading.Event()
    def shutdown(*_args):
        if stopping.is_set(): return
        stopping.set(); threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown); signal.signal(signal.SIGINT,shutdown)
    try: server.serve_forever(poll_interval=.25)
    finally: server.server_close(); bridge.close()
    return 0

if __name__ == "__main__": raise SystemExit(main())
