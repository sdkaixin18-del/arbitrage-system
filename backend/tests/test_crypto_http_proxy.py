from __future__ import annotations

import unittest
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import crypto as crypto_module
from app.crypto import (
    _binance_p2p_median_price,
    api_rate_limit_lane,
    api_request_priority,
    crypto_api_proxy,
    kstr_twenty_day_price_baseline,
    wait_api_rate_limit,
)


class CryptoHttpProxyTest(unittest.TestCase):
    def test_gate_public_market_data_uses_public_rate_limit_bucket(self) -> None:
        url = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
        self.assertEqual(crypto_module.api_rate_bucket_for_url(url), "gt:public")
        self.assertEqual(crypto_module.api_rate_bucket_for_url(url, private=True), "gt:private")

    def test_astro_request_takes_next_shared_rate_limit_slot(self) -> None:
        bucket = "test:astro-priority"
        order: list[str] = []
        background_started = threading.Event()
        crypto_module.API_RATE_LIMIT_INTERVALS_SECONDS[bucket] = 0.03
        with crypto_module._api_rate_limit_condition:
            crypto_module._api_rate_limit_next_at.pop(bucket, None)
            crypto_module._api_rate_limit_backoff_until.pop(bucket, None)
            crypto_module._api_priority_waiters.pop(bucket, None)

        # Occupy the current slot so both worker threads must queue.
        wait_api_rate_limit(bucket)

        def background_worker() -> None:
            background_started.set()
            wait_api_rate_limit(bucket)
            order.append("background")

        def astro_worker() -> None:
            with api_request_priority("astro"):
                wait_api_rate_limit(bucket)
            order.append("astro")

        background = threading.Thread(target=background_worker)
        astro = threading.Thread(target=astro_worker)
        background.start()
        self.assertTrue(background_started.wait(timeout=1))
        time.sleep(0.003)
        astro.start()
        background.join(timeout=1)
        astro.join(timeout=1)

        self.assertFalse(background.is_alive())
        self.assertFalse(astro.is_alive())
        self.assertEqual(order, ["astro", "background"])

        with crypto_module._api_rate_limit_condition:
            crypto_module._api_rate_limit_next_at.pop(bucket, None)
            crypto_module._api_rate_limit_backoff_until.pop(bucket, None)
            crypto_module._api_priority_waiters.pop(bucket, None)
        crypto_module.API_RATE_LIMIT_INTERVALS_SECONDS.pop(bucket, None)

    def test_funding_lane_does_not_wait_for_active_astro_scanner(self) -> None:
        bucket = "test:funding-isolated"
        finished = threading.Event()
        crypto_module.API_RATE_LIMIT_INTERVALS_SECONDS[bucket] = 0.03
        state_key = f"{crypto_module.FUNDING_FORMATION_API_LANE}:{bucket}"
        with crypto_module._api_rate_limit_condition:
            crypto_module._api_rate_limit_next_at.pop(state_key, None)
            crypto_module._api_rate_limit_backoff_until.pop(state_key, None)

        def funding_worker() -> None:
            with api_rate_limit_lane(crypto_module.FUNDING_FORMATION_API_LANE):
                wait_api_rate_limit(bucket)
            finished.set()

        with api_request_priority("astro"):
            worker = threading.Thread(target=funding_worker)
            worker.start()
            self.assertTrue(finished.wait(timeout=0.3))
            worker.join(timeout=0.3)

        self.assertFalse(worker.is_alive())
        with crypto_module._api_rate_limit_condition:
            crypto_module._api_rate_limit_next_at.pop(state_key, None)
            crypto_module._api_rate_limit_backoff_until.pop(state_key, None)
        crypto_module.API_RATE_LIMIT_INTERVALS_SECONDS.pop(bucket, None)

    def test_announcement_lane_does_not_wait_for_active_astro_scanner(self) -> None:
        bucket = "test:announcement-isolated"
        finished = threading.Event()
        crypto_module.API_RATE_LIMIT_INTERVALS_SECONDS[bucket] = 0.03
        state_key = f"{crypto_module.EXCHANGE_ANNOUNCEMENT_API_LANE}:{bucket}"
        with crypto_module._api_rate_limit_condition:
            crypto_module._api_rate_limit_next_at.pop(state_key, None)
            crypto_module._api_rate_limit_backoff_until.pop(state_key, None)

        def announcement_worker() -> None:
            with api_rate_limit_lane(crypto_module.EXCHANGE_ANNOUNCEMENT_API_LANE):
                wait_api_rate_limit(bucket)
            finished.set()

        with api_request_priority("astro"):
            worker = threading.Thread(target=announcement_worker)
            worker.start()
            self.assertTrue(finished.wait(timeout=0.3))
            worker.join(timeout=0.3)

        self.assertFalse(worker.is_alive())
        with crypto_module._api_rate_limit_condition:
            crypto_module._api_rate_limit_next_at.pop(state_key, None)
            crypto_module._api_rate_limit_backoff_until.pop(state_key, None)
        crypto_module.API_RATE_LIMIT_INTERVALS_SECONDS.pop(bucket, None)

    def test_funding_formation_cache_is_bounded(self) -> None:
        original_max = crypto_module.FUNDING_FORMATION_CACHE_MAX_ITEMS
        try:
            crypto_module.FUNDING_FORMATION_CACHE_MAX_ITEMS = 2
            now = datetime.now(ZoneInfo("UTC"))
            with crypto_module._funding_formation_cache_lock:
                crypto_module._funding_formation_cache.clear()
                for index in range(3):
                    crypto_module._funding_formation_cache[("bn", f"TEST{index}", None)] = (
                        now,
                        {"index": index},
                    )
                crypto_module._prune_funding_formation_cache(now)
                keys = list(crypto_module._funding_formation_cache)
            self.assertEqual(keys, [("bn", "TEST1", None), ("bn", "TEST2", None)])
        finally:
            crypto_module.FUNDING_FORMATION_CACHE_MAX_ITEMS = original_max
            with crypto_module._funding_formation_cache_lock:
                crypto_module._funding_formation_cache.clear()

    def test_parses_binance_p2p_median_price(self) -> None:
        value, count = _binance_p2p_median_price(
            {
                "data": [
                    {"adv": {"price": "6.69"}},
                    {"adv": {"price": "6.71"}},
                    {"adv": {"price": "6.70"}},
                ]
            }
        )
        self.assertEqual(value, 6.70)
        self.assertEqual(count, 3)

    def test_rejects_short_binance_p2p_price_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "有效 USDT/CNY 报价不足"):
            _binance_p2p_median_price({"data": [{"adv": {"price": "6.70"}}]})

    def test_explicit_crypto_proxy_takes_priority(self) -> None:
        with (
            patch.dict("os.environ", {"CRYPTO_API_PROXY": "http://127.0.0.1:9000"}),
            patch("app.crypto.urllib.request.getproxies") as getproxies,
        ):
            self.assertEqual(crypto_api_proxy(), "http://127.0.0.1:9000")
            getproxies.assert_not_called()

    def test_uses_macos_system_https_proxy_when_launchagent_has_no_env_proxy(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "app.crypto.urllib.request.getproxies",
                return_value={
                    "https": "http://127.0.0.1:7897",
                    "http": "http://127.0.0.1:7897",
                },
            ),
        ):
            self.assertEqual(crypto_api_proxy(), "http://127.0.0.1:7897")

    def test_returns_none_when_no_proxy_is_available(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("app.crypto.urllib.request.getproxies", return_value={}),
        ):
            self.assertIsNone(crypto_api_proxy())

    def test_calculates_twenty_complete_day_kstr_price_median(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        start = datetime(2026, 7, 1, tzinfo=shanghai)
        etf_rows: dict[int, float] = {}
        kstr_rows: list[dict[str, float]] = []
        for day_offset in range(20):
            day = start + timedelta(days=day_offset)
            sessions = [
                day.replace(hour=9, minute=30) + timedelta(minutes=5 * index)
                for index in range(24)
            ] + [
                day.replace(hour=13, minute=0) + timedelta(minutes=5 * index)
                for index in range(24)
            ]
            for timestamp in sessions:
                ts = int(timestamp.timestamp() * 1000)
                etf_rows[ts] = 2.0
                kstr_rows.append({"ts": ts, "close": 20.0})
        result = kstr_twenty_day_price_baseline(etf_rows, kstr_rows)
        self.assertEqual(result["tradingDays"], 20)
        self.assertEqual(result["sampleCount"], 120)
        self.assertEqual(result["contractUsdPerEtfCnyMedian"], 10.0)
        self.assertEqual(
            result["method"],
            "daily_six_point_median_then_twenty_day_median",
        )
        self.assertEqual(
            result["sampleTimes"],
            ["09:45", "10:30", "11:15", "13:15", "14:00", "14:45"],
        )

    def test_kstr_baseline_ignores_non_signal_intraday_points(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        start = datetime(2026, 7, 1, tzinfo=shanghai)
        signal_times = {"09:45", "10:30", "11:15", "13:15", "14:00", "14:45"}
        etf_rows: dict[int, float] = {}
        kstr_rows: list[dict[str, float]] = []
        for day_offset in range(20):
            day = start + timedelta(days=day_offset)
            sessions = [
                day.replace(hour=9, minute=30) + timedelta(minutes=5 * index)
                for index in range(24)
            ] + [
                day.replace(hour=13, minute=0) + timedelta(minutes=5 * index)
                for index in range(24)
            ]
            for timestamp in sessions:
                ts = int(timestamp.timestamp() * 1000)
                etf_rows[ts] = 2.0
                close = 20.0 if timestamp.strftime("%H:%M") in signal_times else 2000.0
                kstr_rows.append({"ts": ts, "close": close})

        result = kstr_twenty_day_price_baseline(etf_rows, kstr_rows)

        self.assertEqual(result["sampleCount"], 120)
        self.assertEqual(result["contractUsdPerEtfCnyMedian"], 10.0)
        self.assertEqual(result["contractUsdPerEtfCnyDailyMad"], 0.0)

    def test_recent_listing_can_use_provisional_kstr_style_baseline(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        start = datetime(2026, 8, 19, tzinfo=shanghai)
        stock_rows: dict[int, float] = {}
        contract_rows: list[dict[str, float]] = []
        for day_offset in range(3):
            day = start + timedelta(days=day_offset)
            sessions = [
                day.replace(hour=9, minute=30) + timedelta(minutes=5 * index)
                for index in range(24)
            ] + [
                day.replace(hour=13, minute=0) + timedelta(minutes=5 * index)
                for index in range(24)
            ]
            for timestamp in sessions:
                ts = int(timestamp.timestamp() * 1000)
                stock_rows[ts] = 500.0
                contract_rows.append({"ts": ts, "close": 75.0})

        result = kstr_twenty_day_price_baseline(
            stock_rows,
            contract_rows,
            target_days=20,
            minimum_days=3,
            pair_label="Gate UNITREE/688836",
        )

        self.assertEqual(result["tradingDays"], 3)
        self.assertTrue(result["provisional"])
        self.assertEqual(result["contractUsdPerEtfCnyMedian"], 0.15)


if __name__ == "__main__":
    unittest.main()
