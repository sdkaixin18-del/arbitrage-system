"""Persistent SSH supervisor for the isolated read-only trial, not production."""
from __future__ import annotations

import logging
import random
import socket
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


def reconnect_delay(failures: int, jitter: float) -> float:
    return min(60.0, 5.0 * 2 ** min(max(failures - 1, 0), 4)) * (0.8 + 0.2 * jitter)


class PersistentTrialTunnel:
    def __init__(self, host: str, identity: str, local_port: int = 18868):
        if host.startswith("-") or not 1024 <= local_port <= 65535:
            raise ValueError("Invalid trial SSH target")
        self.command = ["ssh", "-N", "-T", "-i", identity,
                        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                        "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=5",
                        "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=10",
                        "-o", "ServerAliveCountMax=2", "-o", "ControlMaster=no",
                        "-o", "ControlPath=none", "-o", "LogLevel=ERROR",
                        "-L", f"127.0.0.1:{local_port}:192.0.2.1:18767", host]
        self.local_port = local_port
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.process = None
        self.attempts = 0
        self.failures = 0
        self.state = "stopped"
        self.retry_at = 0.0
        self.healthy_since = None
        self.last_healthy = None
        self.last_failure_log = None
        self.suppressed_logs = 0

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return
            # Never attach to or kill a listener owned by another service.
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", self.local_port))
            self.stopped.clear()
            self.thread = threading.Thread(target=self._run, name="depth-trial-ssh", daemon=True)
            self.thread.start()

    def mark_healthy(self):
        with self.lock:
            now = time.monotonic()
            if self.healthy_since is None or self.last_healthy is None or now - self.last_healthy > 20:
                self.healthy_since = now
            if self.state != "healthy":
                logger.info("Trial tunnel recovered; suppressed failures=%s", self.suppressed_logs)
                self.suppressed_logs = 0
            self.last_healthy = now
            self.state = "healthy"
            if now - self.healthy_since >= 30:
                self.failures = 0

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "attempts": self.attempts, "failures": self.failures,
                    "retryInSeconds": round(max(0, self.retry_at - time.monotonic()), 2),
                    "pid": self.process.pid if self.process is not None else None}

    def disconnect_for_test(self):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError("No owned trial connection to disconnect")
            self.process.terminate()

    def _run(self):
        try:
            while not self.stopped.is_set():
                with self.lock:
                    self.attempts += 1
                    self.state = "connecting"
                    self.retry_at = 0
                try:
                    process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    with self.lock:
                        self.process = process
                    while process.poll() is None and not self.stopped.wait(.2):
                        pass
                    if self.stopped.is_set():
                        break
                except OSError:
                    pass
                with self.lock:
                    self.process = None
                    self.failures += 1
                    self.healthy_since = self.last_healthy = None
                    self.state = "backoff"
                    delay = reconnect_delay(self.failures, random.random())
                    now = time.monotonic()
                    self.retry_at = now + delay
                    if self.last_failure_log is None or now - self.last_failure_log >= 60:
                        logger.warning("Trial SSH unavailable; retry in %.1fs; repeated failures=%s",
                                       delay, self.suppressed_logs)
                        self.suppressed_logs = 0
                        self.last_failure_log = now
                    else:
                        self.suppressed_logs += 1
                if self.stopped.wait(delay):
                    break
        finally:
            with self.lock:
                process = self.process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            with self.lock:
                self.process = None
                self.retry_at = 0
                self.state = "stopped"

    def stop(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=6)
            if self.thread.is_alive():
                raise RuntimeError("Trial SSH supervisor did not stop")
