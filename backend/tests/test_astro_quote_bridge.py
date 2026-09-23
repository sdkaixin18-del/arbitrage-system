from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import app.crypto as crypto
from app.crypto import _validated_astro_quote_books


def relay_payload(rows: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"snapshot": {"books": {"bn": rows, "bg": {}, "gt": {}, "hl": {}}}}


def test_accepts_fresh_executable_astro_book() -> None:
    now = datetime.now(timezone.utc)
    books, rejected = _validated_astro_quote_books(
        relay_payload({
            "KSTRUSDT": {
                "bid": 24.10,
                "ask": 24.12,
                "sourceUpdatedAt": (now - timedelta(milliseconds=20)).isoformat(),
                "receivedAt": (now - timedelta(milliseconds=10)).isoformat(),
                "source": "Astro Cloud WS · BN KSTRUSDT",
            }
        }),
        {"bn": {"KSTRUSDT"}, "bg": set(), "gt": set(), "hl": set()},
        now,
        2.0,
    )

    assert rejected == []
    assert books["bn"]["KSTRUSDT"]["bid"] == 24.10
    assert books["bn"]["KSTRUSDT"]["ask"] == 24.12


def test_rejects_stale_or_crossed_astro_books() -> None:
    now = datetime.now(timezone.utc)
    books, rejected = _validated_astro_quote_books(
        relay_payload({
            "STALEUSDT": {
                "bid": 10,
                "ask": 11,
                "receivedAt": (now - timedelta(seconds=3)).isoformat(),
            },
            "CROSSEDUSDT": {
                "bid": 12,
                "ask": 11,
                "receivedAt": now.isoformat(),
            },
        }),
        {"bn": {"STALEUSDT", "CROSSEDUSDT"}, "bg": set(), "gt": set(), "hl": set()},
        now,
        2.0,
    )

    assert books["bn"] == {}
    assert sorted(rejected) == ["bn:CROSSEDUSDT", "bn:STALEUSDT"]


def test_removed_all_cards_performs_no_outbound_quote_reads(monkeypatch) -> None:
    monkeypatch.setenv("ASTRO_QUOTE_ENABLED", "0")
    crypto._arb_radar_book_cache.clear()

    def forbidden_http_client(*args, **kwargs):
        raise AssertionError("removed cards must not open an exchange client")

    monkeypatch.setattr(crypto, "http_client", forbidden_http_client)
    payload = crypto.arb_radar_executable_books(set())

    assert payload["status"] == "ok"
    assert payload["books"] == {"bn": {}, "bg": {}, "gt": {}, "hl": {}}


def test_one_visible_card_reads_only_its_exact_book(monkeypatch) -> None:
    monkeypatch.setenv("ASTRO_QUOTE_ENABLED", "0")
    crypto._arb_radar_book_cache.clear()
    calls: list[tuple[str, dict[str, object]]] = []

    @contextmanager
    def fake_http_client(*args, **kwargs):
        yield object()

    def fake_post(client, url, payload):
        calls.append((url, payload))
        return {"time": 1_787_976_528_000, "levels": [[{"px": "10"}], [{"px": "11"}]]}

    monkeypatch.setattr(crypto, "http_client", fake_http_client)
    monkeypatch.setattr(crypto, "request_post_json", fake_post)
    payload = crypto.arb_radar_executable_books({"cxmt-hl"})

    assert calls == [(crypto.base_url("hl") + "/info", {"type": "l2Book", "coin": "xyz:CXMT"})]
    assert set(payload["books"]["hl"]) == {"CXMT"}
    assert payload["books"]["bn"] == {}
    assert payload["books"]["bg"] == {}
    assert payload["books"]["gt"] == {}


def test_enabled_astro_syncs_empty_targets_before_returning(monkeypatch) -> None:
    monkeypatch.setenv("ASTRO_QUOTE_ENABLED", "1")
    crypto._arb_radar_book_cache.clear()
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "subscriptionSynchronized": True,
                "snapshot": {
                    "targets": [],
                    "books": {"bn": {}, "bg": {}, "gt": {}, "hl": {}},
                    "activeConnectionCount": 0,
                    "marketRequestCount": 0,
                    "connections": {},
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url: str, params: dict[str, object]):
            calls.append(params)
            return FakeResponse()

    def forbidden_http_client(*args, **kwargs):
        raise AssertionError("empty targets must not open an exchange client")

    monkeypatch.setattr(crypto.httpx, "Client", FakeClient)
    monkeypatch.setattr(crypto, "http_client", forbidden_http_client)
    payload = crypto.arb_radar_executable_books(set(), "radar-tab", 12)

    assert calls == [{
        "maxAgeMs": 2000,
        "targets": "",
        "consumer": "radar-tab",
        "revision": 12,
    }]
    assert payload["books"] == {"bn": {}, "bg": {}, "gt": {}, "hl": {}}
    assert payload["astro"]["activeConnectionCount"] == 0


def test_enabled_astro_requests_only_visible_card_target(monkeypatch) -> None:
    monkeypatch.setenv("ASTRO_QUOTE_ENABLED", "1")
    crypto._arb_radar_book_cache.clear()
    now = datetime.now(timezone.utc)
    calls: list[dict[str, object]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "subscriptionSynchronized": True,
                "snapshot": {
                    "targets": ["hl:CXMT"],
                    "books": {
                        "bn": {}, "bg": {}, "gt": {},
                        "hl": {"CXMT": {
                            "bid": 10,
                            "ask": 11,
                            "receivedAt": now.isoformat(),
                            "sourceUpdatedAt": now.isoformat(),
                        }},
                    },
                    "activeConnectionCount": 1,
                    "marketRequestCount": 1,
                    "connections": {"hl": {"targets": ["CXMT"]}},
                },
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url: str, params: dict[str, object]):
            calls.append(params)
            return FakeResponse()

    def forbidden_http_client(*args, **kwargs):
        raise AssertionError("complete Astro book must not use public fallback")

    monkeypatch.setattr(crypto.httpx, "Client", FakeClient)
    monkeypatch.setattr(crypto, "http_client", forbidden_http_client)
    payload = crypto.arb_radar_executable_books({"cxmt-hl"}, "radar-tab", 13)

    assert calls[0]["targets"] == "hl:CXMT"
    assert set(payload["books"]["hl"]) == {"CXMT"}
    assert payload["astro"]["usedBooks"] == 1
