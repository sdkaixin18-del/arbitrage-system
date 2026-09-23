from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.kstr_walkforward import (
    FundingEvent,
    QuoteSnapshot,
    ReturnPair,
    WalkForwardConfig,
    build_point_in_time_features,
    compare_beta_methods,
    point_in_time_dynamic_beta,
    run_walk_forward,
)


UTC = timezone.utc


def quote(
    timestamp: datetime,
    ratio: float,
    *,
    beta: float = 1.0,
    beta_asof: datetime | None = None,
    executable: bool = True,
) -> QuoteSnapshot:
    a_mid = 2.0
    kstr_mid = ratio * a_mid / 7.0
    return QuoteSnapshot(
        timestamp=timestamp,
        a_bid=a_mid - 0.001 if executable else None,
        a_ask=a_mid + 0.001 if executable else None,
        kstr_bid=kstr_mid - 0.005 if executable else None,
        kstr_ask=kstr_mid + 0.005 if executable else None,
        usd_cny=7.0,
        dynamic_beta=beta,
        dynamic_beta_asof=beta_asof or timestamp - timedelta(days=1),
    )


class KstrWalkForwardTest(unittest.TestCase):
    def test_future_rows_do_not_change_prior_features(self) -> None:
        start = datetime(2026, 1, 5, 1, 30, tzinfo=UTC)
        base = [quote(start + timedelta(days=index), 90 + index * 0.01) for index in range(8)]
        config = WalkForwardConfig(
            min_baseline_trading_days=2,
            min_baseline_points=3,
            minimum_required_trading_days=1,
            minimum_required_signal_clusters=1,
        )
        before = build_point_in_time_features(base, config)
        after = build_point_in_time_features(
            [*base, quote(start + timedelta(days=8), 150)],
            config,
        )
        self.assertEqual(
            [(row.reference_ratio, row.history_mean_pct, row.history_std_pct) for row in before],
            [(row.reference_ratio, row.history_mean_pct, row.history_std_pct) for row in after[:-1]],
        )

    def test_dynamic_beta_must_be_strictly_point_in_time(self) -> None:
        timestamp = datetime(2026, 1, 5, 1, 30, tzinfo=UTC)
        rows = [
            quote(timestamp + timedelta(days=index), 90 + index * 0.01)
            for index in range(4)
        ]
        rows.append(
            quote(
                timestamp + timedelta(days=4),
                92,
                beta=1.1,
                beta_asof=timestamp + timedelta(days=4),
            )
        )
        config = WalkForwardConfig(
            beta_mode="dynamic",
            min_baseline_trading_days=2,
            min_baseline_points=3,
        )
        features = build_point_in_time_features(rows, config)
        self.assertEqual(features[-1].blocked_reason, "dynamic beta is not strictly prior to the quote")

    def test_t_plus_one_blocks_same_day_exit_and_funding_is_realized(self) -> None:
        day1 = datetime(2026, 1, 5, 1, 30, tzinfo=UTC)
        rows = [quote(day1 - timedelta(days=5 - index), 90 + index * 0.01) for index in range(5)]
        rows.extend(
            [
                quote(day1, 94),
                quote(day1 + timedelta(minutes=1), 89),
                quote(day1 + timedelta(days=1), 89),
            ]
        )
        config = WalkForwardConfig(
            min_baseline_trading_days=3,
            min_baseline_points=5,
            entry_spread_pct=1.0,
            entry_z=0.5,
            exit_spread_pct=0.0,
            max_holding_trading_days=5,
            costs_verified=True,
            minimum_required_trading_days=1,
            minimum_required_signal_clusters=1,
        )
        funding = [FundingEvent(day1 + timedelta(hours=8), -0.001)]
        result = run_walk_forward(rows, funding, config)
        self.assertEqual(result["metrics"]["tradeCount"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["exit_time"], day1 + timedelta(days=1))
        self.assertLess(trade["funding_pnl_cny"], 0)

    def test_missing_books_block_strategy_validation(self) -> None:
        start = datetime(2026, 1, 5, 1, 30, tzinfo=UTC)
        rows = [quote(start + timedelta(days=index), 90, executable=False) for index in range(61)]
        result = run_walk_forward(
            rows,
            [],
            WalkForwardConfig(
                min_baseline_trading_days=2,
                min_baseline_points=3,
                minimum_required_trading_days=60,
                minimum_required_signal_clusters=30,
            ),
        )
        self.assertEqual(result["status"], "insufficient_sample")
        self.assertEqual(result["dataQuality"]["executableBidAskCompletenessPct"], 0)

    def test_sample_gate_requires_sixty_days_and_thirty_clusters(self) -> None:
        start = datetime(2026, 1, 5, 1, 30, tzinfo=UTC)
        rows = [quote(start + timedelta(days=index), 90 + (4 if index % 2 else 0)) for index in range(20)]
        result = run_walk_forward(
            rows,
            [],
            WalkForwardConfig(
                min_baseline_trading_days=2,
                min_baseline_points=3,
                entry_spread_pct=1.0,
                entry_z=0.5,
                minimum_required_trading_days=60,
                minimum_required_signal_clusters=30,
            ),
        )
        self.assertEqual(result["status"], "insufficient_sample")
        self.assertTrue(any("trading days" in reason for reason in result["readinessReasons"]))
        self.assertTrue(any("signal clusters" in reason for reason in result["readinessReasons"]))

    def test_beta_series_never_uses_current_return(self) -> None:
        start = datetime(2026, 1, 5, tzinfo=UTC)
        pairs = [
            ReturnPair(start + timedelta(days=index), 0.01 * (index + 1), 0.01 * (index + 1))
            for index in range(4)
        ]
        before = point_in_time_dynamic_beta(pairs, windows=(2,))
        changed = [
            *pairs[:-1],
            ReturnPair(pairs[-1].timestamp, pairs[-1].a_return, 9.0),
        ]
        after = point_in_time_dynamic_beta(changed, windows=(2,))
        self.assertEqual(before[-1][1], after[-1][1])

    def test_beta_comparison_reports_same_oos_sample(self) -> None:
        start = datetime(2026, 1, 5, tzinfo=UTC)
        pairs = [
            ReturnPair(
                start + timedelta(days=index),
                (index % 7 - 3) / 100,
                (index % 7 - 3) / 100,
            )
            for index in range(10)
        ]
        result = compare_beta_methods(pairs, windows=(3,))
        self.assertEqual(result["fixed"]["sampleCount"], result["dynamic"]["sampleCount"])
        self.assertAlmostEqual(result["fixed"]["residualStdPct"], 0.0)


if __name__ == "__main__":
    unittest.main()
