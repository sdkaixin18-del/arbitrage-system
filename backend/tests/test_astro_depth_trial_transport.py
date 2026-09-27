import socket
import time

import pytest

from app import astro_depth_trial_transport as transport


def test_backoff_grows_and_is_capped():
    assert [transport.reconnect_delay(i, 1) for i in range(1, 8)] == [5, 10, 20, 40, 60, 60, 60]
    assert transport.reconnect_delay(1, 0) == 4


def test_only_authenticated_sustained_health_resets_failures(monkeypatch):
    tunnel = transport.PersistentTrialTunnel("host", "key")
    tunnel.failures = 4
    now = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    tunnel.mark_healthy()
    assert tunnel.failures == 4
    now[0] += 15
    tunnel.mark_healthy()
    now[0] += 15
    tunnel.mark_healthy()
    assert tunnel.failures == 0


def test_health_gap_does_not_reset_failures(monkeypatch):
    tunnel = transport.PersistentTrialTunnel("host", "key")
    tunnel.failures = 4
    now = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    tunnel.mark_healthy()
    now[0] += 100
    tunnel.mark_healthy()
    assert tunnel.failures == 4


def test_port_in_use_is_never_adopted():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        tunnel = transport.PersistentTrialTunnel("host", "key", sock.getsockname()[1])
        with pytest.raises(OSError):
            tunnel.start()
        assert tunnel.thread is None


def test_start_is_singleton_and_stop_interrupts_backoff(monkeypatch):
    class FailedProcess:
        pid = 123
        def poll(self):
            return 255
    calls = []
    def popen(*args, **kwargs):
        calls.append(args)
        return FailedProcess()
    monkeypatch.setattr(transport.subprocess, "Popen", popen)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    tunnel = transport.PersistentTrialTunnel("host", "key", port)
    try:
        tunnel.start()
        tunnel.start()
        deadline = time.monotonic() + 1
        while tunnel.snapshot()["state"] != "backoff" and time.monotonic() < deadline:
            time.sleep(.01)
        assert tunnel.snapshot()["state"] == "backoff"
        assert len(calls) == 1
    finally:
        started = time.monotonic()
        tunnel.stop()
    assert time.monotonic() - started < 1
    assert tunnel.snapshot()["state"] == "stopped"
