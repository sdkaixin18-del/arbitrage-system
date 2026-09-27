import asyncio
import importlib.util
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("depth_trial_probe", Path(__file__).resolve().parents[2] / "scripts/astro_depth_trial_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_hedge_uses_valid_result_not_first_error(monkeypatch):
    async def fetch(client, path, body):
        await asyncio.sleep(0.01 if path == "wg" else 0.03)
        return {"path": path, "ok": path == "ssh", "ms": 30}
    monkeypatch.setattr(probe, "fetch", fetch)
    winner, paths = asyncio.run(probe.race({"wg": None, "ssh": None}, {}, delay=0.001))
    assert winner["path"] == "ssh"
    assert len(paths) == 2


def test_healthy_primary_does_not_launch_backup(monkeypatch):
    called = []
    async def fetch(client, path, body):
        called.append(path)
        return {"path": path, "ok": True, "ms": 1}
    monkeypatch.setattr(probe, "fetch", fetch)
    winner, paths = asyncio.run(probe.race({"wg": None, "ssh": None}, {}))
    assert winner["ok"]
    assert called == ["wg"]


def test_both_failed_is_not_success(monkeypatch):
    async def fetch(client, path, body):
        return {"path": path, "ok": False, "ms": 1}
    monkeypatch.setattr(probe, "fetch", fetch)
    winner, paths = asyncio.run(probe.race({"wg": None, "ssh": None}, {}))
    assert not winner["ok"]
    assert winner["error"] == "both_paths_failed"


def test_ssh_can_be_primary_with_wireguard_backup(monkeypatch):
    called = []
    async def fetch(client, path, body):
        called.append(path)
        return {"path": path, "ok": path == "wg", "ms": 1}
    monkeypatch.setattr(probe, "fetch", fetch)
    winner, paths = asyncio.run(probe.race({"wg": None, "ssh": None}, {}, primary="ssh"))
    assert winner["path"] == "wg"
    assert called == ["ssh", "wg"]


def test_winner_does_not_wait_for_slow_loser(monkeypatch):
    cancelled = []
    async def fetch(client, path, body):
        try:
            await asyncio.sleep(.5 if path == "wg" else .01)
            return {"path": path, "ok": True, "ms": 10}
        except asyncio.CancelledError:
            cancelled.append(path)
            raise
    monkeypatch.setattr(probe, "fetch", fetch)
    started = time.monotonic()
    winner, paths = asyncio.run(probe.race({"wg": None, "ssh": None}, {}, delay=.001))
    assert time.monotonic() - started < .3
    assert winner["path"] == "ssh"
    assert cancelled == ["wg"]
    assert paths[0]["cancelled"]


@pytest.mark.parametrize("change", [
    {"receivedAtMs": 6000}, {"receivedAtMs": 12000}, {"requestId": "wrong"},
    {"statusCode": 429}, {"payload": {"bids": [["nan", "1"]], "asks": [["2", "1"]]}},
    {"payload": {"bids": [["3", "1"]], "asks": [["2", "1"]]}},
])
def test_invalid_depth_never_counts_as_success(change):
    body = {"url": "https://api.binance.com/api/v3/depth", "requestId": "test"}
    envelope = {"service": "astro-depth-cloud", "protocol": 1, "requestId": "test",
                "url": body["url"], "statusCode": 200, "receivedAtMs": 9900,
                "requestStartedAtMs": 9800, "queueWaitMs": 0,
                "payload": {"bids": [["1", "1"]], "asks": [["2", "1"]]}, **change}
    with pytest.raises(ValueError):
        probe.validate(envelope, body, 10000)
