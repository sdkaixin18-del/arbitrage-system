from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "astro_quote_bridge.py"
SPEC = importlib.util.spec_from_file_location("stock_review_astro_quote_bridge", MODULE_PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class FakeClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []
        self.pulse_error: Exception | None = None

    def request(self, targets: tuple[str, ...], max_age_ms: int, timeout: float):
        self.requests.append(targets)
        snapshot = {
            "status": "idle" if not targets else "warming",
            "targets": list(targets),
            "books": {"bn": {}, "bg": {}, "gt": {}, "hl": {}},
            "bookCount": 0,
            "requiredCount": len(targets),
            "activeConnectionCount": len({target.split(":", 1)[0] for target in targets}),
            "marketRequestCount": 0,
            "connections": {},
        }
        return snapshot, 5.0

    def pulse(self, timeout: float):
        if self.pulse_error:
            raise self.pulse_error
        return {
            "status": "ok",
            "payloads": [{"code": 0, "data": {}}],
            "sourceCount": 2,
            "successCount": 1,
            "failureCount": 0,
            "failures": [],
        }, 42.0

    def dex_quote(
        self,
        symbol: str,
        chain_index: str,
        contract_address: str,
        amount_usdt: float,
        timeout: float,
        exchange: str = "okxdex",
    ):
        return {
            "source": "astro_core_okx_v6_quote",
            "symbol": symbol,
            "chainIndex": chain_index,
            "contractAddress": contract_address,
            "amountUsdt": amount_usdt,
            "fromAmount": str(amount_usdt),
            "toAmount": "12.5",
            "tradeFeeUsd": "0.01",
            "quotedAt": 123,
        }, 55.0

    def close(self) -> None:
        return None


def test_dynamic_subscription_applies_exact_targets_and_empty_removal() -> None:
    client = FakeClient()
    relay = bridge.QuoteRelay(client, poll_seconds=0.02, max_age_ms=2_000, lease_seconds=5)
    relay.start()
    try:
        expected, synchronized = relay.update_subscription(
            "radar-tab", 1, ("bn:KSTRUSDT",), wait_timeout=1
        )
        assert synchronized is True
        assert expected == ("bn:KSTRUSDT",)

        # An older aborted request cannot restore a removed target.
        expected, synchronized = relay.update_subscription("radar-tab", 0, (), wait_timeout=1)
        assert synchronized is True
        assert expected == ("bn:KSTRUSDT",)

        expected, synchronized = relay.update_subscription("radar-tab", 2, (), wait_timeout=1)
        assert synchronized is True
        assert expected == ()
        assert client.requests[-1] == ()
        response, healthy = relay.response()
        assert healthy is True
        assert response["desiredTargets"] == []
        assert response["appliedTargets"] == []
    finally:
        relay.close()


def test_subscription_union_keeps_targets_needed_by_another_live_tab() -> None:
    client = FakeClient()
    relay = bridge.QuoteRelay(client, poll_seconds=0.02, max_age_ms=2_000, lease_seconds=5)
    relay.start()
    try:
        relay.update_subscription("tab-a", 1, ("bn:KSTRUSDT",), wait_timeout=1)
        expected, synchronized = relay.update_subscription("tab-b", 1, ("hl:CXMT",), wait_timeout=1)
        assert synchronized is True
        assert expected == ("bn:KSTRUSDT", "hl:CXMT")
        expected, synchronized = relay.update_subscription("tab-a", 2, (), wait_timeout=1)
        assert synchronized is True
        assert expected == ("hl:CXMT",)
    finally:
        relay.close()


def test_rejects_unknown_quote_target() -> None:
    with pytest.raises(ValueError, match="unsupported Astro quote targets"):
        bridge.normalize_targets(("bn:NOT-A-REAL-CARD",))


def test_local_client_executes_quote_gateway_without_ssh() -> None:
    client = bridge.AstroStdioClient(
        "local",
        Path("/does/not/exist"),
        "astro-app",
        "/home/ubuntu/astro-server/quote-gateway.cjs",
    )
    assert client.command() == [
        "/usr/bin/sudo",
        "-n",
        "/usr/bin/docker",
        "exec",
        "-i",
        "-w",
        "/home/ubuntu/astro-server",
        "astro-app",
        "node",
        "/home/ubuntu/astro-server/quote-gateway.cjs",
        "--stdio",
    ]


def test_process_health_tolerates_short_jitter_without_marking_quotes_fresh() -> None:
    relay = bridge.QuoteRelay(FakeClient(), poll_seconds=0.02, max_age_ms=2_000, lease_seconds=5)
    relay.snapshot = {
        "status": "idle",
        "targets": [],
        "books": {"bn": {}, "bg": {}, "gt": {}, "hl": {}},
    }
    relay.snapshot_received_at = time.time() - 3

    response, healthy = relay.response()

    assert healthy is True
    assert response["status"] == "ok"
    assert response["quoteSnapshotFresh"] is False


def test_pulse_health_turns_unhealthy_after_three_failures_and_recovers() -> None:
    client = FakeClient()
    relay = bridge.QuoteRelay(client, poll_seconds=0.02, max_age_ms=2_000, lease_seconds=5)
    client.pulse_error = RuntimeError("temporary cloud failure")

    for _ in range(3):
        with pytest.raises(RuntimeError, match="temporary cloud failure"):
            relay.fetch_pulse()

    assert relay.pulse_status()["healthy"] is False
    assert relay.pulse_status()["consecutiveFailures"] == 3

    client.pulse_error = None
    result = relay.fetch_pulse()

    assert result["payloads"][0]["code"] == 0
    assert relay.pulse_status()["healthy"] is True
    assert relay.pulse_status()["consecutiveFailures"] == 0


def test_pulse_failure_does_not_restart_healthy_book_bridge() -> None:
    client = FakeClient()
    relay = bridge.QuoteRelay(client, poll_seconds=0.02, max_age_ms=2_000, lease_seconds=5)
    relay.snapshot = {
        "status": "idle",
        "targets": [],
        "books": {"bn": {}, "bg": {}, "gt": {}, "hl": {}},
    }
    relay.snapshot_received_at = time.time()
    client.pulse_error = RuntimeError("Pulse only failure")
    for _ in range(3):
        with pytest.raises(RuntimeError):
            relay.fetch_pulse()

    response, healthy = relay.response()

    assert response["pulseHealthy"] is False
    assert response["quoteHealthy"] is True
    assert healthy is True
    assert response["status"] == "ok"


def test_overlapping_pulse_request_is_rejected_without_queueing() -> None:
    relay = bridge.QuoteRelay(FakeClient(), poll_seconds=0.02, max_age_ms=2_000, lease_seconds=5)
    relay.pulse_request_lock.acquire()
    try:
        started = time.perf_counter()
        with pytest.raises(RuntimeError, match="already in progress"):
            relay.fetch_pulse(timeout=6)
        assert time.perf_counter() - started < 0.1
    finally:
        relay.pulse_request_lock.release()


def test_pulse_bridge_decodes_cloud_gzip_payload() -> None:
    class CompressedClient(FakeClient):
        def pulse(self, timeout: float):
            raw = b'[{"code":0,"data":{"binanceFuture":{"ts":1,"list":[]}}}]'
            encoded = bridge.base64.b64encode(bridge.gzip.compress(raw)).decode("ascii")
            return {
                "status": "ok",
                "payloadEncoding": "gzip+base64",
                "payloadsGzipBase64": encoded,
                "sourceCount": 2,
                "successCount": 1,
                "failureCount": 0,
                "failures": [],
            }, 30.0

    relay = bridge.QuoteRelay(
        FakeClient(),
        poll_seconds=0.02,
        max_age_ms=2_000,
        lease_seconds=5,
        pulse_client=CompressedClient(),
    )

    result = relay.fetch_pulse()

    assert result["payloads"][0]["data"]["binanceFuture"]["ts"] == 1
    assert "payloadsGzipBase64" not in result


def test_dex_quote_uses_dedicated_read_only_client_and_returns_rtt() -> None:
    book_client = FakeClient()
    dex_client = FakeClient()
    relay = bridge.QuoteRelay(
        book_client,
        poll_seconds=0.02,
        max_age_ms=2_000,
        lease_seconds=5,
        dex_client=dex_client,
    )

    result = relay.fetch_dex_quote("ABC", "56", "0xabc", 40)

    assert result["source"] == "astro_core_okx_v6_quote"
    assert result["amountUsdt"] == 40
    assert result["toAmount"] == "12.5"
    assert result["cloudRttMs"] == 55.0
    assert result["relayReceivedAt"]


def test_coin_metadata_uses_separate_connection_without_subscriptions():
    import json
    import threading
    from urllib.request import urlopen
    class MetadataClient(FakeClient):
        def _request_command(self, command, timeout):
            assert command == {'command':'dex_coins'}
            return {'coins':[{'name':'ABC','chainIndex':'56','contractAddress':'0x'+'a'*40}]},1
    client=FakeClient();metadata=MetadataClient()
    relay=bridge.QuoteRelay(client,poll_seconds=0.02,max_age_ms=2000,lease_seconds=5,pulse_client=metadata)
    server=bridge.BridgeServer(('127.0.0.1',0),bridge.Handler);server.relay=relay
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with urlopen(f'http://127.0.0.1:{server.server_port}/dex-coins') as response:
            assert json.load(response)['coins'][0]['name']=='ABC'
        assert client.requests == []
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
