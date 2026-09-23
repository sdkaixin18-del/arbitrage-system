from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.funding_formation import (
    analyze_funding_formation,
    analyze_gate_shadow,
    analyze_rolling_reference,
    build_cycle_premium_average_history,
    funding_formation_rule,
    funding_rate_from_average_premium,
    gate_shadow_rule,
    premium_boundary_for_target,
)


class FundingFormationRuleTest(unittest.TestCase):
    def test_premium_average_history_tracks_the_running_formula_mean(self) -> None:
        cycle_start = datetime(2026, 8, 31, 0, tzinfo=timezone.utc)
        now = cycle_start + timedelta(minutes=3)
        rows = [
            {"timestamp": now + timedelta(minutes=1), "close": 0.009},
            {"timestamp": cycle_start + timedelta(minutes=1), "close": 0.003},
            {"timestamp": cycle_start - timedelta(minutes=1), "close": 0.008},
            {"timestamp": cycle_start, "close": 0.001},
        ]
        rule = funding_formation_rule("by", 1)

        self.assertEqual(
            build_cycle_premium_average_history(
                rows,
                rule=rule,
                cycle_start=cycle_start,
                now=now,
            ),
            [
                {
                    "timestamp": cycle_start + timedelta(minutes=1),
                    "averagePremiumRate": 0.001,
                },
                {
                    "timestamp": cycle_start + timedelta(minutes=2),
                    "averagePremiumRate": (0.001 + 0.003 * 2) / 3,
                },
            ],
        )

    def test_bitget_uses_rolling_five_second_linear_weight_rule(self) -> None:
        rule = funding_formation_rule("bg", 4)

        self.assertEqual(rule.sample_seconds, 5)
        self.assertEqual(rule.weighting, "linear")
        self.assertEqual(rule.window_mode, "rolling_interval_reference")
        self.assertEqual(rule.formula_version, "bitget_rolling_linear_5s_verified_20260913")
        self.assertAlmostEqual(rule.interval_scale, 0.5)

    def test_bitget_rolling_reference_is_separate_from_active_cycle(self) -> None:
        now = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)
        rule = funding_formation_rule("bg", 1)
        rows = [
            {
                "timestamp": now - timedelta(hours=1) + timedelta(minutes=index),
                "open": 0.001,
                "high": 0.001,
                "low": 0.001,
                "close": 0.001,
                "representedSamples": 12,
            }
            for index in range(60)
        ]

        result = analyze_rolling_reference(
            rule=rule,
            window_start=now - timedelta(hours=1),
            window_end=now,
            premium_rows=rows,
            floor=-0.01,
            cap=0.01,
        )

        self.assertEqual(result["sampleCount"], 720)
        self.assertEqual(result["historyStatus"], "complete")
        self.assertAlmostEqual(result["averagePremiumRate"], 0.001)

    def test_gate_shadow_applies_dampener_per_minute_before_average(self) -> None:
        cycle_start = datetime(2026, 8, 31, 0, tzinfo=timezone.utc)
        rule = gate_shadow_rule(8)
        rows = [
            {
                "timestamp": cycle_start,
                "open": -0.001,
                "high": -0.001,
                "low": -0.001,
                "close": -0.001,
                "representedSamples": 1,
            },
            {
                "timestamp": cycle_start + timedelta(minutes=1),
                "open": 0.001,
                "high": 0.001,
                "low": 0.001,
                "close": 0.001,
                "representedSamples": 1,
            },
        ]

        result = analyze_gate_shadow(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(hours=8),
            now=cycle_start + timedelta(minutes=2),
            premium_rows=rows,
            floor=-0.01,
            cap=0.01,
        )

        self.assertEqual(result["formulaVersion"], "gate_20260831_per_minute_average_shadow_v1")
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(result["averagePremiumRate"], 0.0)
        self.assertAlmostEqual(result["calculatedFundingRate"], 0.0)
        self.assertNotAlmostEqual(
            result["calculatedFundingRate"],
            funding_rate_from_average_premium(0.0, rule),
        )

    def test_binance_one_hour_uses_eight_over_n_and_equal_weighting(self) -> None:
        rule = funding_formation_rule("bn", 1)
        self.assertEqual(rule.sample_seconds, 5)
        self.assertEqual(rule.weighting, "equal")
        self.assertAlmostEqual(rule.interest_rate, 0.0001)
        self.assertAlmostEqual(rule.interval_scale, 0.125)
        self.assertAlmostEqual(
            funding_rate_from_average_premium(0.0002, rule),
            0.0000125,
        )

    def test_bybit_one_hour_scales_interest_without_eight_over_n(self) -> None:
        rule = funding_formation_rule("by", 1)
        self.assertEqual(rule.sample_seconds, 60)
        self.assertEqual(rule.weighting, "linear")
        self.assertAlmostEqual(rule.interest_rate, 0.0000125)
        self.assertAlmostEqual(rule.interval_scale, 1.0)
        self.assertAlmostEqual(
            funding_rate_from_average_premium(0.0002, rule),
            0.0000125,
        )

    def test_target_inverse_respects_interval_scale_and_damper(self) -> None:
        rule = funding_formation_rule("gt", 4)
        boundary = premium_boundary_for_target(0.00075, rule, floor=-0.003, cap=0.003)
        self.assertEqual(boundary["relation"], "gte")
        self.assertAlmostEqual(boundary["premiumBoundary"], 0.002)

        lower = premium_boundary_for_target(-0.00075, rule, floor=-0.003, cap=0.003)
        self.assertEqual(lower["relation"], "lte")
        self.assertAlmostEqual(lower["premiumBoundary"], -0.002)

    def test_linear_weighted_remaining_threshold(self) -> None:
        cycle_start = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
        cycle_end = cycle_start + timedelta(hours=1)
        now = cycle_start + timedelta(minutes=30)
        rule = funding_formation_rule("by", 1)
        rows = [
            {
                "timestamp": cycle_start + timedelta(minutes=index),
                "open": 0.001,
                "high": 0.001,
                "low": 0.001,
                "close": 0.001,
                "representedSamples": 1,
            }
            for index in range(30)
        ]
        result = analyze_funding_formation(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            now=now,
            premium_rows=rows,
            targets=[
                {
                    "key": "custom",
                    "label": "自定义目标",
                    "targetFundingRate": 0.0005,
                }
            ],
            floor=-0.005,
            cap=0.005,
        )
        self.assertTrue(result["completeHistory"])
        self.assertAlmostEqual(result["averagePremiumRate"], 0.001)
        self.assertAlmostEqual(
            result["targets"][0]["requiredPremiumRate"],
            0.001,
        )

    def test_ohlc_bounds_produce_conservative_requirement(self) -> None:
        cycle_start = datetime(2026, 7, 28, 8, tzinfo=timezone.utc)
        rule = funding_formation_rule("bn", 1)
        result = analyze_funding_formation(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(hours=1),
            now=cycle_start + timedelta(minutes=1),
            premium_rows=[
                {
                    "timestamp": cycle_start,
                    "open": 0.0005,
                    "high": 0.001,
                    "low": 0.0,
                    "close": 0.0005,
                    "representedSamples": 12,
                }
            ],
            targets=[
                {
                    "key": "custom",
                    "label": "自定义目标",
                    "targetFundingRate": 0.0000625,
                }
            ],
            floor=-0.003,
            cap=0.003,
        )
        target = result["targets"][0]
        self.assertTrue(result["completeHistory"])
        self.assertTrue(target["conservative"])
        self.assertGreater(
            target["requiredPremiumRate"],
            target["estimatedRequiredPremiumRate"],
        )

    def test_near_complete_history_estimates_instead_of_blocking(self) -> None:
        cycle_start = datetime(2026, 8, 11, 0, tzinfo=timezone.utc)
        rule = funding_formation_rule("bn", 1)
        rows = [
            {
                "timestamp": cycle_start + timedelta(seconds=index * 5),
                "open": -0.001,
                "high": -0.001,
                "low": -0.001,
                "close": -0.001,
                "representedSamples": 1,
            }
            for index in range(119)
        ]
        result = analyze_funding_formation(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(hours=1),
            now=cycle_start + timedelta(minutes=10),
            premium_rows=rows,
            targets=[
                {
                    "key": "floor",
                    "label": "负向最大费率",
                    "targetFundingRate": -0.0000625,
                }
            ],
            floor=-0.003,
            cap=0.003,
        )

        self.assertFalse(result["completeHistory"])
        self.assertEqual(result["historyStatus"], "estimated")
        self.assertAlmostEqual(result["coverage"], 119 / 120)
        self.assertEqual(result["targets"][0]["status"], "estimated")
        self.assertIsNotNone(result["targets"][0]["estimatedRequiredPremiumRate"])

    def test_history_below_estimation_threshold_remains_insufficient(self) -> None:
        cycle_start = datetime(2026, 8, 11, 0, tzinfo=timezone.utc)
        rule = funding_formation_rule("bn", 1)
        rows = [
            {
                "timestamp": cycle_start + timedelta(seconds=index * 5),
                "open": -0.001,
                "high": -0.001,
                "low": -0.001,
                "close": -0.001,
                "representedSamples": 1,
            }
            for index in range(117)
        ]
        result = analyze_funding_formation(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(hours=1),
            now=cycle_start + timedelta(minutes=10),
            premium_rows=rows,
            targets=[
                {
                    "key": "floor",
                    "label": "负向最大费率",
                    "targetFundingRate": -0.0000625,
                }
            ],
            floor=-0.003,
            cap=0.003,
        )

        self.assertEqual(result["historyStatus"], "insufficient")
        self.assertEqual(result["targets"][0]["status"], "insufficient_history")
        self.assertIsNone(result["predictedFundingRate"])
        self.assertEqual(result["predictionStatus"], "insufficient")

    def test_prediction_shrinks_latest_premium_toward_formed_average(self) -> None:
        cycle_start = datetime(2026, 8, 11, 0, tzinfo=timezone.utc)
        rule = funding_formation_rule("by", 1)
        rows = [
            {
                "timestamp": cycle_start + timedelta(minutes=index),
                "open": 0.001 if index < 29 else 0.003,
                "high": 0.001 if index < 29 else 0.003,
                "low": 0.001 if index < 29 else 0.003,
                "close": 0.001 if index < 29 else 0.003,
                "representedSamples": 1,
            }
            for index in range(30)
        ]
        result = analyze_funding_formation(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(hours=1),
            now=cycle_start + timedelta(minutes=30),
            premium_rows=rows,
            targets=[],
            floor=-0.005,
            cap=0.005,
        )

        self.assertEqual(result["predictionStatus"], "estimated")
        self.assertEqual(result["predictionMethod"], "shrunk_latest_premium_carry_forward_v2")
        self.assertEqual(result["predictionModelVersion"], "funding_formation_v6_bitget_linear")
        self.assertEqual(result["predictionCapMode"], "uncapped_observation")
        self.assertFalse(result["predictionCapApplied"])
        self.assertEqual(result["predictionConfidence"], "low")
        self.assertAlmostEqual(result["predictionLatestPremiumWeight"], 0.85)
        self.assertGreater(
            result["futurePremiumAnchorRate"],
            result["averagePremiumRate"],
        )
        self.assertLess(result["futurePremiumAnchorRate"], 0.003)
        self.assertGreater(
            result["predictedAveragePremiumRate"],
            result["averagePremiumRate"],
        )
        self.assertGreater(
            result["predictedFundingRate"],
            result["calculatedFundingRate"],
        )

    def test_prediction_keeps_raw_upside_above_exchange_cap_for_observation(self) -> None:
        cycle_start = datetime(2026, 8, 11, 0, tzinfo=timezone.utc)
        rule = funding_formation_rule("by", 1)
        rows = [
            {
                "timestamp": cycle_start + timedelta(minutes=index),
                "open": 0.02,
                "high": 0.02,
                "low": 0.02,
                "close": 0.02,
                "representedSamples": 1,
            }
            for index in range(30)
        ]
        result = analyze_funding_formation(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(hours=1),
            now=cycle_start + timedelta(minutes=30),
            premium_rows=rows,
            targets=[],
            floor=-0.005,
            cap=0.005,
        )

        self.assertAlmostEqual(result["calculatedFundingRate"], 0.005)
        self.assertGreater(result["predictedFundingRate"], 0.005)
        self.assertFalse(result["predictionCapApplied"])
        self.assertIn("不应用交易所上限", result["predictionMessage"])

    def test_gate_shadow_prediction_is_uncapped_but_calculated_rate_stays_capped(self) -> None:
        cycle_start = datetime(2026, 8, 31, 0, tzinfo=timezone.utc)
        rule = gate_shadow_rule(8)
        rows = [
            {
                "timestamp": cycle_start + timedelta(minutes=index),
                "open": 0.02,
                "high": 0.02,
                "low": 0.02,
                "close": 0.02,
                "representedSamples": 1,
            }
            for index in range(2)
        ]
        result = analyze_gate_shadow(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(hours=8),
            now=cycle_start + timedelta(minutes=2),
            premium_rows=rows,
            floor=-0.005,
            cap=0.005,
        )

        self.assertAlmostEqual(result["calculatedFundingRate"], 0.005)
        self.assertGreater(result["predictedFundingRate"], 0.005)
        self.assertFalse(result["predictionCapApplied"])

    def test_negative_observation_bypasses_floor_for_all_exchange_formulas(self) -> None:
        start = datetime(2026, 8, 31, 0, tzinfo=timezone.utc)
        rows = [dict(timestamp=start + timedelta(minutes=i), open=-0.08,
                     high=-0.08, low=-0.08, close=-0.08, representedSamples=1)
                for i in range(120)]
        for exchange in ("bn", "by", "gt", "okx", "bg"):
            with self.subTest(exchange=exchange):
                result = analyze_funding_formation(
                    rule=funding_formation_rule(exchange, 4), cycle_start=start,
                    cycle_end=start + timedelta(hours=4),
                    now=start + timedelta(hours=2), premium_rows=[
                        dict(row, representedSamples=60 // funding_formation_rule(exchange, 4).sample_seconds)
                        for row in rows],
                    targets=[], floor=-0.01, cap=0.01)
                self.assertAlmostEqual(result["calculatedFundingRate"], -0.01)
                self.assertLess(result["predictedFundingRate"], -0.01)
                self.assertFalse(result["predictionCapApplied"])
        result = analyze_gate_shadow(
            rule=gate_shadow_rule(8), cycle_start=start,
            cycle_end=start + timedelta(hours=8), now=start + timedelta(hours=2),
            premium_rows=rows, floor=-0.01, cap=0.01)
        self.assertAlmostEqual(result["calculatedFundingRate"], -0.01)
        self.assertLess(result["predictedFundingRate"], -0.01)

    def test_prediction_over_one_hour_is_labeled_scenario(self) -> None:
        cycle_start = datetime(2026, 8, 11, 0, tzinfo=timezone.utc)
        rule = funding_formation_rule("bn", 4)
        rows = [
            {
                "timestamp": cycle_start + timedelta(minutes=index),
                "open": -0.001,
                "high": -0.001,
                "low": -0.001,
                "close": -0.001,
                "representedSamples": 12,
            }
            for index in range(30)
        ]
        result = analyze_funding_formation(
            rule=rule,
            cycle_start=cycle_start,
            cycle_end=cycle_start + timedelta(hours=4),
            now=cycle_start + timedelta(minutes=30),
            premium_rows=rows,
            targets=[],
            floor=-0.02,
            cap=0.02,
        )

        self.assertEqual(result["predictionStatus"], "estimated")
        self.assertEqual(result["predictionConfidence"], "scenario")
        self.assertIn("情景估算", result["predictionMessage"])


if __name__ == "__main__":
    unittest.main()
