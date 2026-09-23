from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.crypto import (
    CoinIndexComponent,
    CoinIndexStatus,
    canonical_exchange_asset_symbol,
    canonicalize_fs_candidate_pool,
    infer_index_component_alias_mapping,
)


class CryptoSymbolAliasScanTest(unittest.TestCase):
    def test_index_components_confirm_differently_named_contract(self) -> None:
        status = CoinIndexStatus(
            exchange="by",
            symbol="NESA",
            status="ok",
            message="ok",
            components=[
                CoinIndexComponent("NES_USDT", 0.42, source="OKX"),
                CoinIndexComponent("NES-USDT", 0.21, source="KuCoin"),
                CoinIndexComponent("NESUSDT", 0.37, source="Bitget"),
            ],
            updated_at=datetime.now(timezone.utc),
        )

        candidate = infer_index_component_alias_mapping("by", "NESA", status, {"NES", "NESA"})

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["inputSymbol"], "NES")
        self.assertEqual(candidate["mappedSymbol"], "NESA")
        self.assertEqual(candidate["priceRatio"], 1.0)

    def test_price_similarity_without_multiple_index_sources_is_rejected(self) -> None:
        status = CoinIndexStatus(
            exchange="by",
            symbol="FAKE",
            status="ok",
            message="ok",
            components=[CoinIndexComponent("REALUSDT", 1.0, source="OnlyOneVenue")],
            updated_at=datetime.now(timezone.utc),
        )

        candidate = infer_index_component_alias_mapping("by", "FAKE", status, {"FAKE", "REAL"})

        self.assertIsNone(candidate)

    def test_mixed_index_basket_is_rejected(self) -> None:
        status = CoinIndexStatus(
            exchange="by",
            symbol="BASKET",
            status="ok",
            message="ok",
            components=[
                CoinIndexComponent("AAAUSDT", 0.5, source="One"),
                CoinIndexComponent("BBBUSDT", 0.5, source="Two"),
            ],
            updated_at=datetime.now(timezone.utc),
        )

        candidate = infer_index_component_alias_mapping("by", "BASKET", status, {"AAA", "BBB", "BASKET"})

        self.assertIsNone(candidate)

    def test_verified_asset_alias_uses_canonical_symbol(self) -> None:
        self.assertEqual(canonical_exchange_asset_symbol("NESA", "by", "futures"), "NES")

    def test_fs_candidate_pool_applies_inverse_execution_mapping(self) -> None:
        candidates = [
            {
                "symbol": "NESA",
                "exchange": "by",
                "fundingRate": -0.01,
                "premiumRate": -0.01,
                "periodHours": 1,
            }
        ]
        inverse = {("by", "futures", "NESA"): "NES"}
        with patch("app.crypto.inverse_symbol_mapping_lookup", return_value=inverse):
            normalized = canonicalize_fs_candidate_pool(object(), candidates)  # type: ignore[arg-type]

        self.assertEqual(normalized[0]["symbol"], "NES")
        self.assertEqual(normalized[0]["exchangeSymbol"], "NESA")


if __name__ == "__main__":
    unittest.main()
