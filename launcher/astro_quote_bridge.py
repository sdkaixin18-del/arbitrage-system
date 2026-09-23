#!/usr/bin/env python3
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
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


DEFAULT_SSH_TARGET = "ubuntu@192.0.2.10"
DEFAULT_SSH_KEY = Path("/home/example/Library/Application Support/stock-review-mac-launcher/secrets/astro.pem")
DEFAULT_CONTAINER = "astro-app"
DEFAULT_GATEWAY_PATH = "/home/ubuntu/astro-server/quote-gateway.cjs"
ALLOWED_TARGETS = frozenset({
    "bn:KSTRUSDT", "bn:MINIMAXUSDT", "bn:SPCXUSDT", "bn:SNDKUSDT",
    "bn:DRAMUSDT", "bn:MUUSDT", "bn:SKHYUSDT", "bn:HOODUSDT",
    "bn:INTCUSDT", "bn:MSTRUSDT", "bn:CXMTUSDT", "bn:UNITREEUSDT",
    "bg:SKHYNIXUSDT", "bg:SKHYUSDT",
    "gt:BOT_USDT", "gt:CXMT_USDT", "gt:UNITREE_USDT",
    "hl:CXMT", "hl:GIGADEV",
})


def bounded_number(value: str | None, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_targets(values: list[str] | tuple[str, ...] | set[str] | frozenset[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(value).strip() for value in values if str(value).strip()}))
    unsupported = [target for target in normalized if target not in ALLOWED_TARGETS]
    if unsupported:
        raise ValueError("unsupported Astro quote targets: " + ", ".join(unsupported))
    return normalized


class AstroStdioClient:
    def __init__(self, target: str, key: Path, container: str, gateway_path: str) -> None:
        self.local = target == "local"
        if not self.local and not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+", target):
            raise ValueError("invalid Astro SSH target")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
            raise ValueError("invalid Astro container name")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", gateway_path):
            raise ValueError("invalid Astro quote gateway path")
        if not self.local and not key.is_file():
            raise FileNotFoundError(f"Astro SSH key does not exist: {key}")
        if not self.local and key.stat().st_mode & 0o077:
            raise PermissionError(f"Astro SSH key must not be group/other-readable: {key}")
        self.target = target
        self.key = key
        self.container = container
        self.gateway_path = gateway_path
        self.process: subprocess.Popen[bytes] | None = None
        self.read_buffer = bytearray()
        self.io_lock = threading.Lock()

    def command(self) -> list[str]:
        if self.local:
            return [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/docker",
                "exec",
                "-i",
                "-w",
                "/home/ubuntu/astro-server",
                self.container,
                "node",
                self.gateway_path,
                "--stdio",
            ]
        remote_command = (
            f"sudo -n docker exec -i -w /home/ubuntu/astro-server {self.container} "
            f"node {self.gateway_path} --stdio"
        )
        return [
            "/usr/bin/ssh",
            "-T",
            "-i",
            str(self.key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=6",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            self.target,
            remote_command,
        ]

    def start(self) -> None:
        self.close()
        self.process = subprocess.Popen(
            self.command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
        self.read_buffer.clear()

    def _readline(self, timeout: float) -> bytes:
        process = self.process
        if process is None or process.stdout is None:
            raise RuntimeError("Astro quote SSH stream is not running")
        deadline = time.monotonic() + timeout
        while True:
            newline = self.read_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self.read_buffer[:newline])
                del self.read_buffer[: newline + 1]
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Astro quote SSH response timed out")
            ready, _, _ = select.select([process.stdout.fileno()], [], [], remaining)
            if not ready:
                raise TimeoutError("Astro quote SSH response timed out")
            chunk = os.read(process.stdout.fileno(), 65_536)
            if not chunk:
                code = process.poll()
                raise ConnectionError(f"Astro quote SSH stream closed (exit={code})")
            self.read_buffer.extend(chunk)

    def _request_command(self, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], float]:
        with self.io_lock:
            if self.process is None or self.process.poll() is not None:
                self.start()
            assert self.process is not None and self.process.stdin is not None
            request_id = uuid.uuid4().hex
            command = json.dumps(
                {"id": request_id, **payload},
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            started = time.perf_counter()
            try:
                self.process.stdin.write(command)
                self.process.stdin.flush()
                while True:
                    line = self._readline(timeout)
                    try:
                        response = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if response.get("id") != request_id:
                        continue
                    if not response.get("ok"):
                        raise RuntimeError(str(response.get("error") or "Astro quote gateway error"))
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise ValueError("Astro quote gateway returned an invalid response")
                    return result, (time.perf_counter() - started) * 1000
            except Exception:
                self.close()
                raise

    def request(
        self,
        targets: tuple[str, ...],
        max_age_ms: int,
        timeout: float,
    ) -> tuple[dict[str, Any], float]:
        return self._request_command(
            {
                "command": "snapshot",
                "maxAgeMs": max_age_ms,
                "targets": list(targets),
            },
            timeout,
        )

    def pulse(self, timeout: float) -> tuple[dict[str, Any], float]:
        return self._request_command(
            {"command": "pulse", "timeoutMs": int(max(1.0, timeout - 0.5) * 1000)},
            timeout,
        )

    def dex_quote(
        self,
        symbol: str,
        chain_index: str,
        contract_address: str,
        amount_usdt: float,
        timeout: float,
        exchange: str = "okxdex",
    ) -> tuple[dict[str, Any], float]:
        return self._request_command(
            {
                "command": "dex_quote",
                "exchange": exchange,
                "symbol": symbol,
                "chainIndex": chain_index,
                "contractAddress": contract_address,
                "amountUsdt": amount_usdt,
            },
            timeout,
        )

    def close(self) -> None:
        process = self.process
        self.process = None
        self.read_buffer.clear()
        if process is None:
            return
        if process.poll() is None and process.stdin:
            try:
                command = json.dumps(
                    {"id": uuid.uuid4().hex, "command": "shutdown"},
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                process.stdin.write(command)
                process.stdin.flush()
                process.wait(timeout=2)
            except (OSError, BrokenPipeError, subprocess.TimeoutExpired):
                pass
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


class QuoteRelay:
    def __init__(
        self,
        client: AstroStdioClient,
        poll_seconds: float,
        max_age_ms: int,
        lease_seconds: float,
        pulse_client: AstroStdioClient | None = None,
        dex_client: AstroStdioClient | None = None,
    ) -> None:
        self.client = client
        # Pulse snapshots can take seconds while book polling runs every
        # 100ms. A dedicated persistent read-only SSH stream prevents the
        # high-frequency book loop from starving a Pulse request on one lock.
        self.pulse_client = pulse_client or client
        self.dex_client = dex_client or client
        self.poll_seconds = poll_seconds
        self.max_age_ms = max_age_ms
        self.lease_seconds = lease_seconds
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.wake_event = threading.Event()
        self.consumers: dict[str, tuple[int, float, tuple[str, ...]]] = {}
        self.desired_targets: tuple[str, ...] = ()
        self.applied_targets: tuple[str, ...] = ()
        self.snapshot: dict[str, Any] | None = None
        self.snapshot_received_at = 0.0
        self.cloud_rtt_ms: float | None = None
        self.last_error: str | None = None
        self.successful_polls = 0
        self.failed_polls = 0
        self.pulse_successful_fetches = 0
        self.pulse_failed_fetches = 0
        self.pulse_consecutive_failures = 0
        self.pulse_last_success_at = 0.0
        self.pulse_last_failure_at = 0.0
        self.pulse_last_rtt_ms: float | None = None
        self.pulse_last_error: str | None = None
        self.pulse_request_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="astro-quote-relay", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        self.client.close()
        if self.pulse_client is not self.client:
            self.pulse_client.close()
        if self.dex_client not in {self.client, self.pulse_client}:
            self.dex_client.close()
        self.thread.join(timeout=5)

    def _prune_and_union_locked(self, now: float) -> bool:
        expired = [
            consumer
            for consumer, (_revision, updated_at, _targets) in self.consumers.items()
            if now - updated_at > self.lease_seconds
        ]
        for consumer in expired:
            del self.consumers[consumer]
        union = normalize_targets([
            target
            for _revision, _updated_at, targets in self.consumers.values()
            for target in targets
        ])
        changed = union != self.desired_targets
        self.desired_targets = union
        return changed

    def update_subscription(
        self,
        consumer: str,
        revision: int,
        targets: tuple[str, ...],
        wait_timeout: float = 1.5,
    ) -> tuple[tuple[str, ...], bool]:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,96}", consumer):
            raise ValueError("invalid Astro quote consumer")
        normalized = normalize_targets(targets)
        with self.condition:
            existing = self.consumers.get(consumer)
            if existing is None or revision >= existing[0]:
                self.consumers[consumer] = (revision, time.monotonic(), normalized)
            changed = self._prune_and_union_locked(time.monotonic())
            expected = self.desired_targets
            if changed or self.applied_targets != expected:
                self.wake_event.set()
            deadline = time.monotonic() + wait_timeout
            while self.applied_targets != expected and not self.stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            return expected, self.applied_targets == expected

    def _targets_for_poll(self) -> tuple[str, ...]:
        with self.condition:
            changed = self._prune_and_union_locked(time.monotonic())
            targets = self.desired_targets
        if changed:
            self.wake_event.set()
        return targets

    def _run(self) -> None:
        retry_seconds = 0.5
        while not self.stop_event.is_set():
            targets = self._targets_for_poll()
            started = time.monotonic()
            try:
                snapshot, rtt_ms = self.client.request(targets, self.max_age_ms, timeout=10.0)
                applied = normalize_targets(snapshot.get("targets") or [])
                with self.condition:
                    self.snapshot = snapshot
                    self.snapshot_received_at = time.time()
                    self.cloud_rtt_ms = rtt_ms
                    self.applied_targets = applied
                    self.last_error = None
                    self.successful_polls += 1
                    self.condition.notify_all()
                retry_seconds = 0.5
                interval = self.poll_seconds if targets else min(1.0, max(0.25, self.poll_seconds * 10))
                remaining = max(0.0, interval - (time.monotonic() - started))
                self.wake_event.wait(remaining)
                self.wake_event.clear()
            except Exception as error:  # noqa: BLE001
                with self.condition:
                    self.last_error = str(error)
                    self.failed_polls += 1
                    self.condition.notify_all()
                self.wake_event.wait(retry_seconds)
                self.wake_event.clear()
                retry_seconds = min(5.0, retry_seconds * 2)

    def fetch_pulse(self, timeout: float = 3.5) -> dict[str, Any]:
        if not self.pulse_request_lock.acquire(blocking=False):
            raise RuntimeError("Astro cloud Pulse request already in progress")
        try:
            result, rtt_ms = self.pulse_client.pulse(timeout)
            payloads = result.get("payloads")
            if result.get("payloadEncoding") == "gzip+base64":
                encoded = result.pop("payloadsGzipBase64", None)
                if not isinstance(encoded, str) or not encoded:
                    raise ValueError("Astro cloud returned an invalid compressed Pulse payload")
                decoded = gzip.decompress(base64.b64decode(encoded, validate=True))
                payloads = json.loads(decoded)
            if not isinstance(payloads, list) or not payloads:
                raise ValueError("Astro cloud returned no Pulse payloads")
            with self.condition:
                self.pulse_successful_fetches += 1
                self.pulse_consecutive_failures = 0
                self.pulse_last_success_at = time.time()
                self.pulse_last_rtt_ms = rtt_ms
                self.pulse_last_error = None
            return {
                **result,
                "payloads": payloads,
                "cloudRttMs": round(rtt_ms, 3),
                "relayReceivedAt": datetime_utc_iso(time.time()),
            }
        except Exception as error:
            with self.condition:
                self.pulse_failed_fetches += 1
                self.pulse_consecutive_failures += 1
                self.pulse_last_failure_at = time.time()
                self.pulse_last_error = str(error)
            raise
        finally:
            self.pulse_request_lock.release()

    def fetch_dex_quote(
        self,
        symbol: str,
        chain_index: str,
        contract_address: str,
        amount_usdt: float,
        timeout: float = 8.0,
        exchange: str = "okxdex",
    ) -> dict[str, Any]:
        result, rtt_ms = self.dex_client.dex_quote(
            symbol,
            chain_index,
            contract_address,
            amount_usdt,
            timeout,
            exchange=exchange,
        )
        return {
            **result,
            "cloudRttMs": round(rtt_ms, 3),
            "relayReceivedAt": datetime_utc_iso(time.time()),
        }

    def pulse_status(self) -> dict[str, Any]:
        with self.condition:
            successes = self.pulse_successful_fetches
            failures = self.pulse_failed_fetches
            consecutive = self.pulse_consecutive_failures
            last_success_at = self.pulse_last_success_at
            last_failure_at = self.pulse_last_failure_at
            last_rtt_ms = self.pulse_last_rtt_ms
            last_error = self.pulse_last_error
        attempted = successes + failures > 0
        healthy = not attempted or consecutive < 3
        return {
            "healthy": healthy,
            "attempted": attempted,
            "successfulFetches": successes,
            "failedFetches": failures,
            "consecutiveFailures": consecutive,
            "lastSuccessAt": datetime_utc_iso(last_success_at),
            "lastFailureAt": datetime_utc_iso(last_failure_at),
            "lastCloudRttMs": round(last_rtt_ms, 3) if last_rtt_ms is not None else None,
            "lastError": last_error,
            "failureThreshold": 3,
        }

    def response(self) -> tuple[dict[str, Any], bool]:
        with self.condition:
            changed = self._prune_and_union_locked(time.monotonic())
            if changed:
                self.wake_event.set()
            snapshot = json.loads(json.dumps(self.snapshot)) if self.snapshot is not None else None
            received_at = self.snapshot_received_at
            rtt_ms = self.cloud_rtt_ms
            error = self.last_error
            successes = self.successful_polls
            failures = self.failed_polls
            desired_targets = self.desired_targets
            applied_targets = self.applied_targets
            consumer_count = len(self.consumers)
        relay_age_ms = max(0.0, (time.time() - received_at) * 1000) if received_at else None
        synchronized = desired_targets == applied_targets
        quote_snapshot_fresh = relay_age_ms is not None and relay_age_ms <= self.max_age_ms
        # Process supervision must tolerate a short SSH/cloud jitter. Quote
        # acceptance stays stricter: the backend independently rejects every
        # executable book older than ASTRO_QUOTE_MAX_AGE_MS.
        quote_healthy = (
            snapshot is not None
            and relay_age_ms is not None
            and relay_age_ms <= max(10_000, self.max_age_ms * 5)
            and synchronized
        )
        pulse = self.pulse_status()
        # Pulse has its own client and reconnects on every failed command. A
        # Pulse-only failure must not restart the whole bridge and interrupt
        # otherwise healthy executable books. The scanner races its independent
        # local direct path and still exposes Pulse health separately.
        healthy = quote_healthy
        return {
            "status": "ok" if healthy else "warming",
            "relayReceivedAt": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(received_at))
                + f".{int(received_at % 1 * 1000):03d}Z"
                if received_at
                else None
            ),
            "relayAgeMs": round(relay_age_ms, 3) if relay_age_ms is not None else None,
            "cloudRttMs": round(rtt_ms, 3) if rtt_ms is not None else None,
            "successfulPolls": successes,
            "failedPolls": failures,
            "consumerCount": consumer_count,
            "leaseSeconds": self.lease_seconds,
            "desiredTargets": list(desired_targets),
            "appliedTargets": list(applied_targets),
            "subscriptionSynchronized": synchronized,
            "quoteSnapshotFresh": quote_snapshot_fresh,
            "quoteHealthy": quote_healthy,
            "pulseHealthy": pulse["healthy"],
            "pulse": pulse,
            "lastError": error,
            "snapshot": snapshot,
        }, healthy


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    relay: QuoteRelay


class Handler(BaseHTTPRequestHandler):
    server: BridgeServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in {"/health", "/books", "/pulse", "/dex-quote", "/dex-coins"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/dex-coins":
            # Metadata uses the separate Pulse connection, never the executable quote queue.
            try:
                response, _ = self.server.relay.pulse_client._request_command({"command": "dex_coins"}, 4.0)
                status = HTTPStatus.OK
            except Exception:
                response = {"error": "Astro coin configuration unavailable"}
                status = HTTPStatus.SERVICE_UNAVAILABLE
            payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/dex-quote":
            symbol = query.get("symbol", [""])[0].strip().upper()
            chain_index = query.get("chainIndex", [""])[0].strip()
            contract_address = query.get("contractAddress", [""])[0].strip()
            amount_usdt = bounded_number(query.get("amountUsdt", [None])[0], 0, 0, 10_000)
            try:
                if not symbol or not chain_index or not contract_address or amount_usdt <= 0:
                    raise ValueError("missing OKXDEX quote parameters")
                response = self.server.relay.fetch_dex_quote(
                    symbol,
                    chain_index,
                    contract_address,
                    amount_usdt,
                    exchange=query.get("exchange", ["okxdex"])[0],
                )
                status = HTTPStatus.OK
            except ValueError as error:
                response = {"status": "error", "error": str(error)}
                status = HTTPStatus.BAD_REQUEST
            except Exception as error:  # noqa: BLE001
                response = {"status": "error", "error": str(error)}
                status = HTTPStatus.SERVICE_UNAVAILABLE
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/pulse":
            timeout = bounded_number(query.get("timeoutSeconds", [None])[0], 3.5, 1.5, 8.5)
            try:
                response = self.server.relay.fetch_pulse(timeout)
                status = HTTPStatus.OK
            except Exception as error:  # noqa: BLE001
                response = {
                    "status": "error",
                    "error": str(error),
                    "pulse": self.server.relay.pulse_status(),
                }
                status = HTTPStatus.SERVICE_UNAVAILABLE
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/books":
            raw_targets = query.get("targets", [""])[0]
            targets = normalize_targets(tuple(target for target in raw_targets.split(",") if target))
            consumer = query.get("consumer", ["arb-radar"])[0]
            raw_revision = query.get("revision", [str(time.time_ns())])[0]
            try:
                revision = int(raw_revision)
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "invalid subscription revision")
                return
            try:
                self.server.relay.update_subscription(consumer, revision, targets)
            except ValueError as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error))
                return
        response, healthy = self.server.relay.response()
        if parsed.path == "/books":
            requested_age = query.get("maxAgeMs", [None])[0]
            response["requestedMaxAgeMs"] = int(bounded_number(requested_age, 5_000, 500, 60_000))
        else:
            snapshot = response.pop("snapshot", None)
            response["bookCount"] = snapshot.get("bookCount") if isinstance(snapshot, dict) else 0
            response["requiredCount"] = snapshot.get("requiredCount") if isinstance(snapshot, dict) else 0
            response["gatewayStatus"] = snapshot.get("status") if isinstance(snapshot, dict) else "warming"
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response_healthy = response.get("quoteHealthy", healthy) if parsed.path == "/books" else healthy
        self.send_response(HTTPStatus.OK if response_healthy else HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def datetime_utc_iso(timestamp: float) -> str | None:
    if not timestamp:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(timestamp)) + f".{int(timestamp % 1 * 1000):03d}Z"


def main() -> int:
    parser = argparse.ArgumentParser(description="Local read-only relay for Astro cloud quotes")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ASTRO_QUOTE_BRIDGE_PORT", "8765")))
    args = parser.parse_args()
    target = os.environ.get("ASTRO_QUOTE_SSH_TARGET") or os.environ.get(
        "ASTRO_MANUAL_ORDER_SSH_TARGET", DEFAULT_SSH_TARGET
    )
    key = Path(os.environ.get("ASTRO_QUOTE_SSH_KEY") or os.environ.get(
        "ASTRO_MANUAL_ORDER_SSH_KEY", str(DEFAULT_SSH_KEY)
    )).expanduser()
    container = os.environ.get("ASTRO_QUOTE_CONTAINER") or os.environ.get(
        "ASTRO_MANUAL_ORDER_CONTAINER", DEFAULT_CONTAINER
    )
    gateway_path = os.environ.get("ASTRO_QUOTE_GATEWAY_PATH", DEFAULT_GATEWAY_PATH)
    poll_ms = bounded_number(os.environ.get("ASTRO_QUOTE_POLL_MS"), 100, 50, 5_000)
    max_age_ms = int(bounded_number(os.environ.get("ASTRO_QUOTE_MAX_AGE_MS"), 5_000, 500, 60_000))
    lease_seconds = bounded_number(os.environ.get("ASTRO_QUOTE_LEASE_SECONDS"), 45, 5, 300)
    relay = QuoteRelay(
        AstroStdioClient(target, key, container, gateway_path),
        poll_seconds=poll_ms / 1000,
        max_age_ms=max_age_ms,
        lease_seconds=lease_seconds,
        pulse_client=AstroStdioClient(target, key, container, gateway_path),
        dex_client=AstroStdioClient(target, key, container, gateway_path),
    )
    relay.start()
    server = BridgeServer((args.host, args.port), Handler)
    server.relay = relay
    shutdown_started = threading.Event()

    def request_shutdown(*_args: object) -> None:
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        threading.Thread(target=server.shutdown, name="astro-quote-http-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        relay.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
