from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.dex_history import (
    _fetch_binance_rows,
    _fetch_best_token_pool,
    _fetch_dex_pool_metadata,
    _granularity,
    align_spread_points,
    astro_symmetric_spread_pct,
    normalize_futures_symbol,
    normalize_futures_venue,
    normalize_dex_network,
    normalize_pool_address,
)


class DexHistoryTest(unittest.TestCase):
    def test_astro_symmetric_rule_is_equal_and_opposite_when_legs_swap(self) -> None:
        dex_buy_aster_sell = astro_symmetric_spread_pct(100.0, 102.0)
        aster_buy_dex_sell = astro_symmetric_spread_pct(102.0, 100.0)

        self.assertAlmostEqual(dex_buy_aster_sell, 2 * (102 - 100) / (102 + 100) * 100)
        self.assertAlmostEqual(aster_buy_dex_sell, -dex_buy_aster_sell)

    def test_aligns_only_matching_cloud_kline_timestamps(self) -> None:
        points = align_spread_points(
            {1000: 100.0, 2000: 102.0, 3000: 104.0},
            {1000: 101.0, 2500: 999.0, 3000: 103.0},
        )

        self.assertEqual([point["timestamp"] for point in points], [1000, 3000])
        self.assertAlmostEqual(points[0]["spreadPct"], 2 * (101 - 100) / 201 * 100)
        self.assertAlmostEqual(points[1]["spreadPct"], 2 * (103 - 104) / 207 * 100)

    def test_accepts_pool_address_or_geckoterminal_link(self) -> None:
        address = "HHNPd8DpneXbaYBiGC7SU28mYxocbkMznPABPN3sGTyU"
        self.assertEqual(normalize_pool_address(address), address)
        self.assertEqual(
            normalize_pool_address(f"https://www.geckoterminal.com/solana/pools/{address}?utm_source=test"),
            address,
        )

    def test_accepts_evm_pool_address_for_supported_chains(self) -> None:
        address = "0x1234567890abcdef1234567890abcdef12345678"
        self.assertEqual(normalize_pool_address(address, "bsc"), address)
        self.assertEqual(
            normalize_pool_address(f"https://www.geckoterminal.com/base/pools/{address}", "base"),
            address,
        )

    def test_normalizes_uppercase_evm_address_before_pool_matching(self) -> None:
        mixed_case = "0XF280B16EF293D8E534E370794EF26BF312694126"
        canonical = "0xf280b16ef293d8e534e370794ef26bf312694126"
        self.assertEqual(normalize_pool_address(mixed_case, "eth"), canonical)
        self.assertEqual(
            normalize_pool_address(
                f"https://www.geckoterminal.com/eth/pools/{mixed_case}",
                "eth",
            ),
            canonical,
        )

    def test_rejects_link_from_a_different_selected_chain(self) -> None:
        address = "0x1234567890abcdef1234567890abcdef12345678"
        with self.assertRaisesRegex(ValueError, "不一致"):
            normalize_pool_address(f"https://www.geckoterminal.com/eth/pools/{address}", "arbitrum")

    def test_normalizes_all_supported_dex_network_names(self) -> None:
        self.assertEqual(normalize_dex_network("BNB Smart Chain"), "bsc")
        self.assertEqual(normalize_dex_network("Ethereum"), "eth")

    def test_normalizes_coin_name_for_selected_futures_venue(self) -> None:
        self.assertEqual(normalize_futures_symbol("intc", "Binance"), ("INTC", "INTCUSDT"))
        self.assertEqual(normalize_futures_symbol("BOT/USDT", "Gate"), ("BOT", "BOT_USDT"))
        self.assertEqual(normalize_futures_symbol("ETH-USDT-SWAP", "okx"), ("ETH", "ETH-USDT-SWAP"))

    def test_accepts_all_requested_futures_venue_codes(self) -> None:
        expected = {
            "bg": "bg",
            "bn": "bn",
            "gt": "gt",
            "okx": "okx",
            "aster": "aster",
            "by": "by",
        }
        self.assertEqual({value: normalize_futures_venue(value) for value in expected}, expected)

    def test_detects_requested_token_side_from_pool_metadata(self) -> None:
        client = Mock()
        response = Mock()
        response.is_success = True
        response.json.return_value = {
            "data": {
                "attributes": {"name": "INTC / USDC"},
                "relationships": {
                    "base_token": {"data": {"id": "base-id"}},
                    "quote_token": {"data": {"id": "quote-id"}},
                },
            },
            "included": [
                {"id": "base-id", "attributes": {"symbol": "INTC"}},
                {"id": "quote-id", "attributes": {"symbol": "USDC"}},
            ],
        }
        client.get.return_value = response

        metadata = _fetch_dex_pool_metadata(client, "pool", "INTC")

        self.assertEqual(metadata["tokenSide"], "base")
        self.assertEqual(metadata["poolName"], "INTC / USDC")

    def test_token_address_chooses_highest_liquidity_pool(self) -> None:
        token_address = "TokenAddress111111111111111111111111111111"
        token_id = f"solana_{token_address}"
        client = Mock()
        response = Mock()
        response.is_success = True
        response.json.return_value = {
            "included": [{"id": token_id, "attributes": {"symbol": "FONE"}}],
            "data": [
                {
                    "attributes": {"address": "small-pool", "name": "FONE / USDC", "reserve_in_usd": "10"},
                    "relationships": {
                        "base_token": {"data": {"id": token_id}},
                        "quote_token": {"data": {"id": "quote"}},
                    },
                },
                {
                    "attributes": {"address": "large-pool", "name": "FONE / SOL", "reserve_in_usd": "100"},
                    "relationships": {
                        "base_token": {"data": {"id": token_id}},
                        "quote_token": {"data": {"id": "quote"}},
                    },
                },
            ],
        }
        client.get.return_value = response

        pool_address, metadata = _fetch_best_token_pool(client, "solana", token_address, "FONE")

        self.assertEqual(pool_address, "large-pool")
        self.assertEqual(metadata["poolName"], "FONE / SOL")

    def test_evm_token_pool_matches_canonical_lowercase_resource_id(self) -> None:
        token_address = normalize_pool_address(
            "0XF280B16EF293D8E534E370794EF26BF312694126",
            "eth",
        )
        token_id = f"eth_{token_address}"
        client = Mock()
        response = Mock()
        response.is_success = True
        response.json.return_value = {
            "included": [{"id": token_id, "attributes": {"symbol": "ASTEROID"}}],
            "data": [
                {
                    "attributes": {
                        "address": "0x76a411f14a704099ba476ce8dffc288a53295218",
                        "name": "ASTEROID / WETH",
                        "reserve_in_usd": "1709200",
                    },
                    "relationships": {
                        "base_token": {"data": {"id": token_id}},
                        "quote_token": {"data": {"id": "eth_weth"}},
                    },
                }
            ],
        }
        client.get.return_value = response

        pool_address, metadata = _fetch_best_token_pool(client, "eth", token_address, "ASTEROID")

        self.assertEqual(pool_address, "0x76a411f14a704099ba476ce8dffc288a53295218")
        self.assertEqual(metadata["tokenSide"], "base")

    def test_selects_granularity_from_requested_range(self) -> None:
        self.assertEqual(_granularity(24), ("1m", 60_000))
        self.assertEqual(_granularity(168), ("5m", 300_000))
        self.assertEqual(_granularity(720), ("15m", 900_000))

    def test_rejects_invalid_pool_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "Solana"):
            normalize_pool_address("INTC")

    def test_binance_pagination_stops_when_latest_candle_reaches_end(self) -> None:
        client = Mock()
        response = Mock()
        response.is_success = True
        response.json.return_value = [
            [0, "1", "1", "1", "1"],
            [60_000, "1", "1", "1", "1.1"],
            [120_000, "1", "1", "1", "1.2"],
        ]
        client.get.return_value = response

        rows = _fetch_binance_rows(client, "TESTUSDT", 0, 150_000, "1m", 60_000)

        self.assertEqual(len(rows), 3)
        self.assertEqual(client.get.call_count, 1)

    def test_binance_invalid_symbol_has_actionable_message(self) -> None:
        client = Mock()
        response = Mock()
        response.is_success = False
        response.status_code = 400
        response.json.return_value = {"code": -1121, "msg": "Invalid symbol."}
        client.get.return_value = response

        with self.assertRaisesRegex(RuntimeError, "Binance 未找到 FONEUSDT 永续合约"):
            _fetch_binance_rows(client, "FONEUSDT", 0, 150_000, "1m", 60_000)


if __name__ == "__main__":
    unittest.main()
