from __future__ import annotations

import unittest

from app.asset_aliases import (
    apply_price_ratio,
    load_asset_alias_config,
    resolve_canonical_asset,
    resolve_exchange_symbol,
    validate_asset_alias_config,
)


class AssetAliasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_asset_alias_config()

    def test_exchange_mapping_keeps_face_value_as_execution_alias(self) -> None:
        resolved = resolve_exchange_symbol("NEX", "bg", "spot", config=self.config)
        self.assertEqual(resolved.status, "verified")
        self.assertEqual(resolved.canonical_symbol, "NEX")
        self.assertEqual(resolved.request_symbol, "10000NEX")
        self.assertEqual(resolved.price_ratio, 10000)
        self.assertEqual(apply_price_ratio(123400.0, resolved.price_ratio), 12.34)

    def test_face_value_symbol_resolves_to_base_asset_without_new_asset(self) -> None:
        resolved = resolve_canonical_asset("10000NEX", self.config)
        self.assertEqual(resolved.canonical_symbol, "NEX")
        self.assertEqual(resolved.canonical_asset_id, "asset:nex")
        self.assertIn(resolved.status, {"verified", "inferred"})

    def test_same_symbol_different_assets_requires_manual_review(self) -> None:
        resolved = resolve_canonical_asset("EDGE", self.config)
        self.assertEqual(resolved.status, "manual_review")
        self.assertFalse(resolved.usable_for_scan)
        self.assertGreaterEqual(len(resolved.candidates), 2)

    def test_wrapped_asset_is_not_collapsed_into_native_asset(self) -> None:
        eth = resolve_canonical_asset("ETH", self.config)
        weth = resolve_canonical_asset("WETH", self.config)
        self.assertEqual(eth.canonical_asset_id, "asset:eth")
        self.assertEqual(weth.canonical_asset_id, "asset:weth")
        self.assertNotEqual(eth.canonical_asset_id, weth.canonical_asset_id)

    def test_cross_chain_asset_retains_network_metadata(self) -> None:
        assets = {asset["canonicalAssetId"]: asset for asset in self.config["assets"]}
        usdt = assets["asset:usdt"]
        networks = {item["network"] for item in usdt["networks"]}
        self.assertIn("ethereum", networks)
        self.assertIn("tron", networks)
        resolved = resolve_canonical_asset("USDT", self.config)
        self.assertEqual(resolved.canonical_asset_id, "asset:usdt")

    def test_migration_alias_requires_explicit_evidence(self) -> None:
        old_symbol = resolve_canonical_asset("MATIC", self.config)
        new_symbol = resolve_canonical_asset("POL", self.config)
        self.assertEqual(old_symbol.canonical_asset_id, "asset:polygon-ecosystem-token")
        self.assertEqual(new_symbol.canonical_asset_id, "asset:polygon-ecosystem-token")
        self.assertEqual(new_symbol.canonical_symbol, "POL")

    def test_nesa_exchange_alias_resolves_to_nes(self) -> None:
        canonical = resolve_canonical_asset("NESA", self.config)
        execution = resolve_exchange_symbol("NES", "by", "futures", config=self.config)
        self.assertEqual(canonical.canonical_symbol, "NES")
        self.assertEqual(canonical.canonical_asset_id, "asset:nesa")
        self.assertEqual(execution.request_symbol, "NESA")
        self.assertEqual(execution.price_ratio, 1.0)

    def test_unknown_asset_is_not_automatically_trusted(self) -> None:
        resolved = resolve_canonical_asset("NOTAREALLOCALCOIN", self.config)
        self.assertEqual(resolved.status, "manual_review")
        self.assertFalse(resolved.usable_for_scan)

    def test_config_has_no_hard_errors(self) -> None:
        issues = validate_asset_alias_config(self.config)
        errors = [issue for issue in issues if issue.get("level") == "error"]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
