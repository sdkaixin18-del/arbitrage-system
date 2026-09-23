from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.crypto as crypto_module
from app.crypto import (
    a_share_cash_market_open,
    a_share_kstr_refresh_window_active,
    a_share_market_phase,
    a_share_open_auction_observation_active,
    append_negative_candidate,
    borrow_period_rate,
    capture_kstr_auction_observations,
    cached_fs_spot_check_for_quote_failure,
    compute_crypto_fs_signals_overview,
    contract_candle_rows,
    contract_candle_rows_page,
    contract_ticker_price,
    pair_spread_exchange,
    pair_spread_granularity,
    pair_spread_item,
    pair_spread_market_symbol,
    pair_spread_symmetric_pct,
    percentile,
    probe_missing_fs_futures_routes,
    kstr_etf_similarity_metrics,
    kstr_liquid_etf_options,
    kstr_contract_market_snapshot,
    kstr_order_book_spread_values,
    load_kstr_auction_backfill_audit,
    load_kstr_auction_ticks,
    record_kstr_auction_tick,
    kstr_session_comparison,
    kstr_bid1_spread_values,
    kstr_symmetric_normalized_spread_pct,
    load_kstr_page_snapshot,
    save_kstr_page_snapshot,
    start_kstr_page_snapshot_refresh,
    fetch_bitget_uta_margin_short_check,
    fetch_bitget_fs_fast_inventory_check,
    fetch_binance_fs_margin_short_check,
    fetch_binance_next_hourly_interest_rates,
    fetch_binance_transfer_status,
    fetch_fs_market_quote,
    evaluate_fs_spot_exchange,
    fs_astro_spread_rate,
    fs_futures_routes_by_symbol,
    fs_signal_large_spread,
    fs_binance_fast_inventory_check,
    harmonize_fs_transfer_statuses,
    mark_interrupted_fs_runtime_scans,
    CoinTransferStatus,
    MarginShortCheck,
    MarketQuote,
    normalize_kstr_a_etf_code,
    normalize_kstr_contract_exchange,
    normalize_fs_signals_payload,
    merge_fs_fast_borrow_checks,
    merge_fs_fast_market_quote,
    crypto_pair_spread_latest,
    crypto_kstr_a_share_spread_page,
    crypto_sk_hynix_us_kr_spread,
    fs_observation_summary,
    funding_cap_rate_changed,
    funding_cap_event_to_out,
    funding_interval_changed,
    funding_formation_directional_target,
    funding_rule_change_kinds,
    fetch_funding_cap_check,
    normalize_funding_cap_watch_exchanges,
    refresh_funding_cap_watchlist,
    update_funding_cap_watch_exchanges,
    fs_runtime_logs_overview,
    record_fs_candidate_runtime_log,
    resolve_hyperliquid_pair_coin,
    select_fs_signal_scan_candidates,
    select_okx_funding_scan_instruments,
    single_factor_return_stats,
    sk_hynix_stock_history,
    stock_quote_number,
    synchronized_return_beta,
    update_fs_signal_caches_from_fast_scan,
    write_fs_borrow_cache,
    is_synchronized_kstr_spread_point,
    usd_cny_quote,
)
from app.models import (
    CryptoFsObservationLog,
    CryptoFsRuntimeLog,
    CryptoFundingCapEvent,
    CryptoFundingCapSnapshot,
    CryptoFundingCapWatchItem,
)


class PairSpreadTest(unittest.TestCase):
    def test_funding_formation_can_fall_back_to_recent_valid_result(self) -> None:
        now = datetime.now(timezone.utc)
        cached_payload = {
            "status": "ok",
            "updatedAt": now - timedelta(minutes=1),
            "symbol": "BTC",
        }

        result = crypto_module.stale_funding_formation_payload(
            (now - timedelta(minutes=2), cached_payload),
            now=now,
            error="TLS timeout",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "partial_error")
        self.assertTrue(result["stale"])
        self.assertIn("TLS timeout", result["errorMessage"])
        self.assertEqual(cached_payload["status"], "ok")

    def test_kstr_spread_response_is_trimmed_by_a_share_trading_day(self) -> None:
        payload = {
            "status": "ok",
            "items": [
                {"time": "2026-07-24T01:20:00Z", "source": "auction"},
                {"time": "2026-07-24T01:31:00Z", "source": "history"},
                {"time": "2026-07-27T01:20:00Z", "source": "auction"},
                {"time": "2026-07-27T01:31:00Z", "source": "history"},
                {"time": "2026-07-28T01:20:00Z", "source": "auction"},
                {"time": "2026-07-28T01:31:00Z", "source": "history"},
                {"time": "2026-07-28T01:35:00Z", "source": "history"},
            ],
        }

        result = crypto_module.kstr_spread_payload_for_range(payload, 2)

        self.assertEqual(result["rangeDays"], 2)
        self.assertEqual(result["rangeTradingDays"], 2)
        self.assertEqual(result["totalItemCount"], 7)
        self.assertEqual(result["returnedItemCount"], 4)
        self.assertEqual(
            [row["time"] for row in result["items"]],
            [
                "2026-07-27T01:20:00Z",
                "2026-07-27T01:31:00Z",
                "2026-07-28T01:20:00Z",
                "2026-07-28T01:35:00Z",
            ],
        )
        self.assertEqual(len(payload["items"]), 7)

    def test_kstr_spread_response_keeps_accumulated_live_minutes(self) -> None:
        payload = {
            "status": "ok",
            "historyGranularityMinutes": 1,
            "items": [
                {
                    "time": "2026-07-30T01:30:00Z",
                    "source": "history",
                    "marketPhase": "continuous",
                    "spreadPct": 0.8,
                },
                {
                    "time": "2026-07-30T01:30:50Z",
                    "source": "live",
                    "marketPhase": "continuous",
                    "spreadPct": 1.0,
                },
                {
                    "time": "2026-07-30T01:31:40Z",
                    "source": "live",
                    "marketPhase": "continuous",
                    "spreadPct": 1.2,
                },
                {
                    "time": "2026-07-30T01:32:30Z",
                    "source": "live",
                    "marketPhase": "continuous",
                    "spreadPct": 1.4,
                },
            ],
        }

        result = crypto_module.kstr_spread_payload_for_range(payload, 1, 1)

        self.assertEqual(result["sourceHistoryGranularityMinutes"], 1)
        self.assertEqual(result["historyGranularityMinutes"], 1)
        self.assertEqual(
            [row["time"] for row in result["items"]],
            [
                "2026-07-30T01:30:50Z",
                "2026-07-30T01:31:40Z",
                "2026-07-30T01:32:30Z",
            ],
        )
        self.assertEqual(
            [row["spreadPct"] for row in result["items"]],
            [1.0, 1.2, 1.4],
        )

        five_minute_result = crypto_module.kstr_spread_payload_for_range(
            payload,
            1,
            5,
        )
        self.assertEqual(five_minute_result["sourceHistoryGranularityMinutes"], 1)
        self.assertEqual(five_minute_result["historyGranularityMinutes"], 5)

    def test_funding_formation_uses_one_direction_from_current_rate(self) -> None:
        negative_targets, negative_floor, negative_cap = funding_formation_directional_target(
            -0.001,
            0.005,
        )
        self.assertEqual(len(negative_targets), 1)
        self.assertEqual(negative_targets[0]["key"], "floor")
        self.assertEqual(negative_targets[0]["targetFundingRate"], -0.005)
        self.assertEqual(negative_floor, -0.005)
        self.assertEqual(negative_cap, 0.005)

        positive_targets, _, _ = funding_formation_directional_target(0.001, -0.005)
        self.assertEqual(len(positive_targets), 1)
        self.assertEqual(positive_targets[0]["key"], "cap")
        self.assertEqual(positive_targets[0]["targetFundingRate"], 0.005)

        custom_targets, _, _ = funding_formation_directional_target(-0.001, 0.005, 0.002)
        self.assertEqual(custom_targets[0]["key"], "custom")
        self.assertEqual(custom_targets[0]["targetFundingRate"], 0.002)

    def test_funding_cap_watch_is_idle_without_symbols_and_alerts_only_after_baseline(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        CryptoFundingCapWatchItem.__table__.create(engine)
        CryptoFundingCapSnapshot.__table__.create(engine)
        CryptoFundingCapEvent.__table__.create(engine)

        with Session(engine) as db:
            with patch.object(crypto_module, "fetch_fs_futures_market_symbols") as market_symbols:
                empty = refresh_funding_cap_watchlist(db, push=True)
            market_symbols.assert_not_called()
            self.assertFalse(empty["monitoring"])
            self.assertEqual(empty["itemCount"], 0)

            item = CryptoFundingCapWatchItem(symbol="TLM", exchanges_json='["bn"]', enabled=True)
            db.add(item)
            db.commit()
            current_cap = {"value": 0.0075}
            checked_exchanges: list[str] = []

            def cap_check(_client, exchange: str, _symbol: str, _market_symbols):
                checked_exchanges.append(exchange)
                if exchange != "bn":
                    return {
                        "exchange": exchange,
                        "status": "not_supported",
                        "message": "未上线",
                        "maxFundingRate": None,
                        "minFundingRate": None,
                        "fundingIntervalHours": None,
                        "checkedAt": datetime.now(timezone.utc),
                    }
                return {
                    "exchange": exchange,
                    "status": "ok",
                    "message": "8h",
                    "maxFundingRate": current_cap["value"],
                    "minFundingRate": -current_cap["value"],
                    "fundingIntervalHours": 8,
                    "checkedAt": datetime.now(timezone.utc),
                }

            with (
                patch.object(crypto_module, "fetch_fs_futures_market_symbols", return_value={"bn": {"TLM"}}),
                patch.object(crypto_module, "fetch_funding_cap_check", side_effect=cap_check),
                patch.object(crypto_module, "send_bark_or_log", return_value=("ok", None)) as bark,
            ):
                baseline = refresh_funding_cap_watchlist(db, push=True)
                self.assertEqual(len(list(db.scalars(select(CryptoFundingCapEvent)))), 0)
                bark.assert_not_called()
                self.assertTrue(baseline["monitoring"])

                current_cap["value"] = 0.02
                changed = refresh_funding_cap_watchlist(db, push=True)

            events = list(db.scalars(select(CryptoFundingCapEvent)))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].previous_max_funding_rate, 0.0075)
            self.assertEqual(events[0].current_max_funding_rate, 0.02)
            self.assertEqual(changed["scan"]["changedCount"], 1)
            self.assertEqual(changed["scan"]["pushedCount"], 1)
            self.assertEqual(checked_exchanges, ["bn", "bn"])

    def test_funding_cap_change_requires_two_real_values(self) -> None:
        self.assertFalse(funding_cap_rate_changed(None, 0.01))
        self.assertFalse(funding_cap_rate_changed(0.01, None))
        self.assertFalse(funding_cap_rate_changed(0.01, 0.01000000001))
        self.assertTrue(funding_cap_rate_changed(0.01, 0.02))

    def test_funding_interval_change_requires_two_real_values(self) -> None:
        self.assertFalse(funding_interval_changed(None, 1))
        self.assertFalse(funding_interval_changed(8, None))
        self.assertFalse(funding_interval_changed(8, 8.000000001))
        self.assertTrue(funding_interval_changed(8, 1))

    def test_funding_rule_ignores_floor_only_change(self) -> None:
        self.assertEqual(
            funding_rule_change_kinds(0.01, 0.01, -0.01, -0.02, 8, 8),
            [],
        )
        self.assertEqual(
            funding_rule_change_kinds(0.01, 0.02, -0.01, -0.01, 8, 8),
            ["cap"],
        )
        self.assertEqual(
            funding_rule_change_kinds(0.01, 0.01, -0.01, -0.01, 8, 1),
            ["interval"],
        )

    def test_funding_rule_watch_records_interval_change_without_alert(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        CryptoFundingCapWatchItem.__table__.create(engine)
        CryptoFundingCapSnapshot.__table__.create(engine)
        CryptoFundingCapEvent.__table__.create(engine)

        with Session(engine) as db:
            item = CryptoFundingCapWatchItem(
                symbol="TLM",
                exchanges_json='["bn"]',
                enabled=True,
            )
            db.add(item)
            db.commit()
            current_interval = {"value": 8.0}

            def rule_check(_client, exchange: str, _symbol: str, _market_symbols):
                return {
                    "exchange": exchange,
                    "status": "ok",
                    "message": f"{current_interval['value']:g}h",
                    "maxFundingRate": 0.01,
                    "minFundingRate": -0.01,
                    "fundingIntervalHours": current_interval["value"],
                    "checkedAt": datetime.now(timezone.utc),
                }

            with (
                patch.object(
                    crypto_module,
                    "fetch_fs_futures_market_symbols",
                    return_value={"bn": {"TLM"}},
                ),
                patch.object(
                    crypto_module,
                    "fetch_funding_cap_check",
                    side_effect=rule_check,
                ),
                patch.object(
                    crypto_module,
                    "send_bark_or_log",
                    return_value=("ok", None),
                ) as bark,
            ):
                refresh_funding_cap_watchlist(db, push=True)
                bark.assert_not_called()
                current_interval["value"] = 1.0
                changed = refresh_funding_cap_watchlist(db, push=True)

            events = list(db.scalars(select(CryptoFundingCapEvent)))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].previous_funding_interval_hours, 8.0)
            self.assertEqual(events[0].funding_interval_hours, 1.0)
            self.assertEqual(
                funding_cap_event_to_out(events[0])["changeKinds"],
                ["interval"],
            )
            self.assertEqual(changed["scan"]["changedCount"], 1)
            self.assertFalse(events[0].pushed)
            self.assertEqual(events[0].push_status, "suppressed")
            self.assertIn("结算周期变化按规则仅记录", events[0].push_message)
            bark.assert_not_called()

    @patch("app.crypto.request_json")
    def test_funding_rule_watch_reads_aster_cap_floor_and_interval(
        self,
        request_json,
    ) -> None:
        request_json.return_value = [
            {
                "symbol": "CXMTUSDT",
                "fundingIntervalHours": 1,
                "fundingFeeCap": 0.02,
                "fundingFeeFloor": -0.02,
            }
        ]
        result = fetch_funding_cap_check(
            object(),
            "as",
            "CXMT",
            {"as": {"CXMT"}},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["maxFundingRate"], 0.02)
        self.assertEqual(result["minFundingRate"], -0.02)
        self.assertEqual(result["fundingIntervalHours"], 1)
        self.assertIn("1h", result["message"])

    def test_funding_cap_exchange_selection_requires_supported_route(self) -> None:
        self.assertEqual(
            normalize_funding_cap_watch_exchanges(["gt", "bn", "gt"]),
            ["bn", "gt"],
        )
        self.assertEqual(
            normalize_funding_cap_watch_exchanges(["as"]),
            ["as"],
        )
        with self.assertRaisesRegex(ValueError, "至少选择一个交易所"):
            normalize_funding_cap_watch_exchanges([])
        with self.assertRaisesRegex(ValueError, "仅支持"):
            normalize_funding_cap_watch_exchanges(["hl"])

    def test_funding_cap_watch_updates_to_selected_exchange_only(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        CryptoFundingCapWatchItem.__table__.create(engine)
        CryptoFundingCapSnapshot.__table__.create(engine)
        CryptoFundingCapEvent.__table__.create(engine)

        with Session(engine) as db:
            item = CryptoFundingCapWatchItem(
                symbol="CXMT",
                exchanges_json='["bn","gt"]',
                enabled=True,
            )
            db.add(item)
            db.flush()
            db.add_all(
                [
                    CryptoFundingCapSnapshot(
                        watch_item_id=item.id,
                        symbol="CXMT",
                        exchange="bn",
                        status="not_supported",
                    ),
                    CryptoFundingCapSnapshot(
                        watch_item_id=item.id,
                        symbol="CXMT",
                        exchange="gt",
                        status="ok",
                        max_funding_rate=0.01,
                        min_funding_rate=-0.01,
                    ),
                ]
            )
            db.commit()
            checked_exchanges: list[str] = []

            def cap_check(_client, exchange: str, _symbol: str, _market_symbols):
                checked_exchanges.append(exchange)
                return {
                    "exchange": exchange,
                    "status": "ok",
                    "message": "8h",
                    "maxFundingRate": 0.01,
                    "minFundingRate": -0.01,
                    "fundingIntervalHours": 8,
                    "checkedAt": datetime.now(timezone.utc),
                }

            with (
                patch.object(crypto_module, "fetch_fs_futures_market_symbols", return_value={"gt": {"CXMT"}}),
                patch.object(crypto_module, "fetch_funding_cap_check", side_effect=cap_check),
            ):
                result = update_funding_cap_watch_exchanges(db, "CXMT", ["gt"])

            self.assertEqual(result["items"][0]["selectedExchanges"], ["gt"])
            self.assertEqual(checked_exchanges, ["gt"])
            self.assertEqual(
                [snapshot.exchange for snapshot in db.scalars(select(CryptoFundingCapSnapshot))],
                ["gt"],
            )

    def test_fs_runtime_log_records_candidate_exchange_result(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        CryptoFsRuntimeLog.__table__.create(engine)
        with Session(engine) as db:
            record_fs_candidate_runtime_log(
                db,
                "scan-test",
                {
                    "symbol": "RIF",
                    "futuresExchange": "gt",
                    "spotExchange": "bn",
                    "currentFundingRate": -0.003,
                    "reason": "当前无 B，保留观察。",
                    "_runtimeDurationMs": 125.4,
                    "checks": {
                        "bn": {
                            "status": "not_borrowable",
                            "message": "当前账户新增可借额度为 0",
                            "canBorrow": False,
                            "inventoryAvailable": False,
                        },
                        "bg": {
                            "status": "not_supported",
                            "message": "Client error 400 Bad Request",
                        },
                    },
                },
            )
            db.commit()
            overview = fs_runtime_logs_overview(db, scan_id="scan-test")

        self.assertEqual(overview["count"], 1)
        row = overview["items"][0]
        self.assertEqual(row["symbol"], "RIF")
        self.assertEqual(row["futuresExchange"], "gt")
        self.assertEqual(row["spotExchange"], "bn")
        self.assertEqual(row["status"], "partial_error")
        self.assertEqual(row["durationMs"], 125.4)
        self.assertEqual(row["details"]["checks"]["bg"]["status"], "not_supported")

    def test_fs_runtime_log_marks_unfinished_scan_after_restart(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        CryptoFsRuntimeLog.__table__.create(engine)
        with Session(engine) as db:
            crypto_module.add_fs_runtime_log(
                db,
                "scan-interrupted",
                "scan_started",
                status="running",
                message="FS 后台扫描开始。",
            )
            db.commit()
            self.assertEqual(mark_interrupted_fs_runtime_scans(db), 1)
            self.assertEqual(mark_interrupted_fs_runtime_scans(db), 0)
            overview = fs_runtime_logs_overview(db, scan_id="scan-interrupted")

        self.assertEqual([row["eventType"] for row in reversed(overview["items"])], ["scan_started", "scan_interrupted"])
        self.assertEqual(overview["items"][0]["level"], "error")

    @patch("app.crypto._compute_crypto_fs_signals_overview")
    def test_fs_runtime_log_persists_scan_failure(self, compute_mock) -> None:
        compute_mock.side_effect = RuntimeError("上游接口超时")
        engine = create_engine("sqlite:///:memory:")
        CryptoFsRuntimeLog.__table__.create(engine)
        with Session(engine) as db:
            with self.assertRaisesRegex(RuntimeError, "上游接口超时"):
                compute_crypto_fs_signals_overview(db, 20, push=True)
            overview = fs_runtime_logs_overview(db, limit=20)

        self.assertEqual([row["eventType"] for row in reversed(overview["items"])], ["scan_started", "scan_failed"])
        self.assertEqual(overview["items"][0]["level"], "error")
        self.assertIn("上游接口超时", overview["items"][0]["message"])

    def test_fs_astro_spread_matches_open_and_close_formula(self) -> None:
        self.assertAlmostEqual(fs_astro_spread_rate(0.0780, 0.0744), 2 * (0.0780 - 0.0744) / (0.0780 + 0.0744))
        self.assertAlmostEqual(fs_astro_spread_rate(100.5, 100.0), 2 * (100.5 - 100.0) / (100.5 + 100.0))
        self.assertIsNone(fs_astro_spread_rate(None, 100.0))

    def test_fs_limit_order_spread_threshold_is_one_percent(self) -> None:
        self.assertTrue(fs_signal_large_spread(0.01))
        self.assertTrue(fs_signal_large_spread(-0.01))
        self.assertFalse(fs_signal_large_spread(0.0099))

    def test_fs_futures_routes_are_aggregated_by_symbol(self) -> None:
        routes = fs_futures_routes_by_symbol(
            [
                {"symbol": "TLM", "exchange": "gt"},
                {"symbol": "TLM", "exchange": "bg"},
                {"symbol": "RIF", "exchange": "gt"},
            ],
            {
                "bn": {"TLM", "RIF"},
                "by": set(),
                "gt": {"TLM", "RIF"},
                "bg": {"TLM"},
            },
        )
        self.assertEqual(routes["TLM"], ("bn", "gt", "bg"))
        self.assertEqual(routes["RIF"], ("bn", "gt"))

    @patch("app.crypto.fetch_fs_market_quote")
    def test_fs_futures_routes_probe_exchange_when_market_list_failed(self, fetch_quote_mock) -> None:
        fetch_quote_mock.return_value = MarketQuote(
            exchange="bn",
            symbol="RIF",
            market_type="futures",
            status="ok",
            best_bid=0.078,
            best_ask=0.079,
        )
        routes = probe_missing_fs_futures_routes(
            {"RIF": ("gt",)},
            [{"symbol": "RIF", "exchange": "gt"}],
            {"gt": {"RIF"}, "by": set(), "okx": set(), "bg": set()},
        )
        self.assertEqual(routes["RIF"], ("bn", "gt"))

    def test_fs_scan_keeps_borrowable_and_strongest_negative_candidates(self) -> None:
        selected = select_fs_signal_scan_candidates(
            [
                {"exchange": "bn", "symbol": "BORROWABLE", "fundingRate": -0.0001, "premiumRate": -0.0001, "periodHours": 8},
                {"exchange": "gt", "symbol": "RIF", "fundingRate": -0.0055, "premiumRate": -0.04, "periodHours": 1},
                {"exchange": "by", "symbol": "MILD", "fundingRate": -0.0002, "premiumRate": -0.0002, "periodHours": 8},
            ],
            {"BORROWABLE"},
            6,
        )
        selected_keys = {(item["exchange"], item["symbol"]) for item in selected}
        self.assertIn(("bn", "BORROWABLE"), selected_keys)
        self.assertIn(("gt", "RIF"), selected_keys)

    def test_fs_scan_reserves_strongest_distinct_symbols(self) -> None:
        selected = select_fs_signal_scan_candidates(
            [
                {"exchange": "bn", "symbol": "DEXE", "fundingRate": -0.02, "periodHours": 1},
                {"exchange": "gt", "symbol": "DEXE", "fundingRate": -0.019, "periodHours": 1},
                {"exchange": "bg", "symbol": "DEXE", "fundingRate": -0.018, "periodHours": 1},
                {"exchange": "bn", "symbol": "RIF", "fundingRate": -0.0034, "periodHours": 1},
                {"exchange": "bn", "symbol": "BARD", "fundingRate": -0.012, "periodHours": 4},
                {"exchange": "bn", "symbol": "BORROWABLE", "fundingRate": -0.0001, "periodHours": 8},
            ],
            {"BORROWABLE"},
            5,
        )
        selected_symbols = {item["symbol"] for item in selected}
        self.assertIn("RIF", selected_symbols)
        self.assertIn("BARD", selected_symbols)

    def test_okx_funding_scan_prioritizes_previous_negative_and_active_then_rotates(self) -> None:
        instruments = [{"instId": f"COIN{index}-USDT-SWAP"} for index in range(8)]
        tickers = {
            f"COIN{index}-USDT-SWAP": {"volCcy24h": str(index * 1000)}
            for index in range(8)
        }
        selected, cursor = select_okx_funding_scan_instruments(
            instruments,
            tickers,
            {"COIN0-USDT-SWAP"},
            active_limit=2,
            rotation_limit=2,
            rotation_cursor=0,
            max_contracts=5,
        )
        self.assertIn("COIN0-USDT-SWAP", selected)
        self.assertIn("COIN7-USDT-SWAP", selected)
        self.assertIn("COIN6-USDT-SWAP", selected)
        self.assertEqual(len(selected), 5)
        self.assertEqual(cursor, 2)

        selected_next, cursor_next = select_okx_funding_scan_instruments(
            instruments,
            tickers,
            {"COIN0-USDT-SWAP"},
            active_limit=2,
            rotation_limit=2,
            rotation_cursor=cursor,
            max_contracts=5,
        )
        self.assertNotEqual(set(selected), set(selected_next))
        self.assertEqual(cursor_next, 4)

    def test_fs_spot_quote_failure_uses_recent_cached_prices(self) -> None:
        write_fs_borrow_cache(
            "RIFCACHE",
            "bg",
            {
                "status": "not_borrowable",
                "message": "Bitget 支持但当前新增可借为 0",
                "canBorrow": False,
                "inventoryAvailable": False,
                "spotBid": 0.0806,
                "spotAsk": 0.0807,
            },
            source="test",
        )
        cached = cached_fs_spot_check_for_quote_failure(
            "RIFCACHE",
            "bg",
            0.07863,
            0.07864,
            -0.0034,
            "盘口超时",
        )
        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertTrue(cached["quoteCached"])
        self.assertGreater(cached["openSpreadRate"], 0.02)

    @patch("app.crypto.fetch_market")
    def test_fs_market_quote_retries_once_when_book_is_missing(self, fetch_market) -> None:
        fetch_market.side_effect = [
            MarketQuote(exchange="bg", symbol="RIF", market_type="spot", status="error", error="timeout"),
            MarketQuote(
                exchange="bg",
                symbol="RIF",
                market_type="spot",
                status="ok",
                best_bid=0.0806,
                best_ask=0.0807,
            ),
        ]
        quote = fetch_fs_market_quote("bg", "RIF", "spot")
        self.assertEqual(quote.status, "ok")
        self.assertEqual(quote.best_bid, 0.0806)
        self.assertEqual(fetch_market.call_count, 2)

    def test_fs_spot_bad_request_is_reported_as_unlisted_market_not_system_error(self) -> None:
        message = crypto_module.fs_spot_quote_failure_message(
            "bg",
            "Client error '400 Bad Request' for url 'https://api.bitget.com/api/v2/spot/market/tickers?symbol=NONEUSDT'",
        )
        self.assertEqual(message, "Bitget 现货未上线或无有效盘口。")
        self.assertEqual(crypto_module.fs_spot_quote_failure_message("bg", "timed out"), "timed out")

    def test_hyperliquid_pair_spread_mapping(self) -> None:
        self.assertEqual(pair_spread_exchange("hl"), "hl")
        self.assertEqual(pair_spread_market_symbol("hl", "btc"), "BTC")
        self.assertEqual(pair_spread_market_symbol("hl", "xyz:SKHX"), "xyz:SKHX")
        self.assertEqual(pair_spread_granularity("hl", "1H"), "1h")

    @patch("app.crypto.hyperliquid_pair_markets")
    def test_hyperliquid_hip3_symbol_is_resolved_automatically(self, hyperliquid_pair_markets) -> None:
        hyperliquid_pair_markets.return_value = ("BTC", "xyz:SKHX", "xyz:SKHY")
        self.assertEqual(resolve_hyperliquid_pair_coin(object(), "SKHX"), "xyz:SKHX")
        self.assertEqual(resolve_hyperliquid_pair_coin(object(), "XYZ:skhx"), "xyz:SKHX")

    @patch("app.crypto.hyperliquid_pair_markets")
    def test_hyperliquid_unknown_symbol_has_readable_error(self, hyperliquid_pair_markets) -> None:
        hyperliquid_pair_markets.return_value = ("BTC", "xyz:SKHX")
        with self.assertRaisesRegex(ValueError, "未找到合约 UNKNOWN"):
            resolve_hyperliquid_pair_coin(object(), "UNKNOWN")

    @patch("app.crypto.request_post_json")
    def test_hyperliquid_ticker_uses_executable_book(self, request_post_json) -> None:
        request_post_json.return_value = {
            "time": 1_700_000_000_000,
            "levels": [[{"px": "99", "sz": "2"}], [{"px": "101", "sz": "3"}]],
        }
        row = contract_ticker_price(object(), "hl", "BTC")
        self.assertEqual(row["close"], 100)
        self.assertEqual(row["bid"], 99)
        self.assertEqual(row["ask"], 101)
        self.assertEqual(request_post_json.call_args.args[2], {"type": "l2Book", "coin": "BTC"})

    @patch("app.crypto.request_post_json")
    def test_hyperliquid_candles_use_snapshot_endpoint(self, request_post_json) -> None:
        request_post_json.return_value = [
            {"t": 1_700_000_000_000, "c": "100"},
            {"t": 1_700_000_060_000, "c": "102"},
        ]
        rows = contract_candle_rows_page(object(), "hl", "BTC", "1m", 20, 1_700_000_000_000, 1_700_000_060_000)
        self.assertEqual([row["close"] for row in rows], [100, 102])
        payload = request_post_json.call_args.args[2]
        self.assertEqual(payload["type"], "candleSnapshot")
        self.assertEqual(payload["req"]["coin"], "BTC")
        self.assertEqual(payload["req"]["interval"], "1m")

    @patch("app.crypto.contract_candle_rows_page")
    def test_internal_candle_gap_is_backfilled_from_one_minute_close(self, candle_page) -> None:
        base = 1_700_000_000_000
        candle_page.side_effect = [
            [
                {"ts": base, "close": 100.0},
                {"ts": base + 30 * 60_000, "close": 103.0},
            ],
            [
                {"ts": base + 15 * 60_000, "close": 101.0},
                {"ts": base + 29 * 60_000, "close": 102.0},
            ],
        ]
        rows = contract_candle_rows(
            object(),
            "bg",
            "SKHY",
            "15m",
            3,
            base,
            base + 30 * 60_000,
        )
        self.assertEqual([row["ts"] for row in rows], [base, base + 15 * 60_000, base + 30 * 60_000])
        self.assertEqual(rows[1]["close"], 102.0)
        self.assertEqual(rows[1]["backfilledFrom"], "1m")
        self.assertEqual(candle_page.call_args_list[1].args[3], "1m")

    def test_screenshot_prices_match_astro_symmetric_spread(self) -> None:
        spread = pair_spread_symmetric_pct(173.12, 147.73)
        self.assertIsNotNone(spread)
        self.assertEqual(round(spread or 0, 2), 15.83)

    def test_realtime_open_and_close_use_executable_quotes(self) -> None:
        row = pair_spread_item(
            1_700_000_000_000,
            173.12,
            1477.3,
            10,
            "ticker",
            left_bid=173.10,
            left_ask=173.14,
            right_bid_raw=1477.2,
            right_ask_raw=1477.5,
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertAlmostEqual(row["openSpreadPct"], pair_spread_symmetric_pct(173.10, 147.75))
        self.assertAlmostEqual(row["closeSpreadPct"], pair_spread_symmetric_pct(173.14, 147.72))
        self.assertAlmostEqual(row["rawRatio"], 1477.3 / 173.12)

    @patch("app.crypto.contract_ticker_price")
    def test_pair_spread_latest_uses_recent_success_on_transport_timeout(self, contract_ticker_price_mock) -> None:
        contract_ticker_price_mock.side_effect = [
            {"close": 10.0, "bid": 9.9, "ask": 10.1, "ts": 1_700_000_000_000},
            {"close": 102.0, "bid": 101.0, "ask": 103.0, "ts": 1_700_000_000_000},
        ]
        live = crypto_pair_spread_latest("CACHELEFT", "CACHERIGHT", 10, "bg", "bg")
        self.assertEqual(live["status"], "ok")

        contract_ticker_price_mock.side_effect = httpx.ConnectTimeout("timeout")
        cached = crypto_pair_spread_latest("CACHELEFT", "CACHERIGHT", 10, "bg", "bg")
        self.assertEqual(cached["status"], "partial_error")
        self.assertEqual(cached["source"], "cached_contract_ticker")
        self.assertEqual(cached["latest"], live["latest"])
        self.assertIn("自动重试", cached["message"])

    @patch("app.crypto.threading.Thread")
    def test_fs_background_scan_clears_previous_finished_time(self, thread_mock) -> None:
        limit = 97
        with crypto_module._fs_signal_cache_lock:
            crypto_module._fs_signal_scan_state[limit] = {
                "running": False,
                "startedAt": datetime.now(timezone.utc) - timedelta(minutes=2),
                "finishedAt": datetime.now(timezone.utc) - timedelta(minutes=1),
                "push": False,
            }
        try:
            self.assertTrue(crypto_module.start_fs_signal_background_scan(limit, push=False))
            with crypto_module._fs_signal_cache_lock:
                state = dict(crypto_module._fs_signal_scan_state[limit])
            self.assertTrue(state["running"])
            self.assertIsNone(state["finishedAt"])
            thread_mock.return_value.start.assert_called_once()
        finally:
            with crypto_module._fs_signal_cache_lock:
                crypto_module._fs_signal_scan_state.pop(limit, None)

    @patch("app.crypto.save_fs_signal_good_cache")
    def test_fs_completed_scan_is_published_to_shared_cache(self, save_cache_mock) -> None:
        limit = 96
        started_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        finished_at = datetime.now(timezone.utc)
        payload = crypto_module.empty_crypto_fs_signals_overview(limit)
        previous_cache = dict(crypto_module._fs_signal_cache)
        previous_state = dict(crypto_module._fs_signal_scan_state)
        try:
            crypto_module.begin_fs_signal_scan(limit, started_at=started_at)
            result = crypto_module.cache_fs_signal_scan_result(
                limit,
                payload,
                started_at=started_at,
                finished_at=finished_at,
            )

            with crypto_module._fs_signal_cache_lock:
                cached_at, cached_payload = crypto_module._fs_signal_cache[limit]
                state = dict(crypto_module._fs_signal_scan_state[limit])
            self.assertEqual(cached_at, finished_at)
            self.assertEqual(cached_payload["cacheVersion"], crypto_module.FS_SIGNAL_CACHE_VERSION)
            self.assertFalse(state["running"])
            self.assertEqual(state["startedAt"], started_at)
            self.assertEqual(state["finishedAt"], finished_at)
            self.assertFalse(result["scanning"])
            save_cache_mock.assert_called_once()
        finally:
            with crypto_module._fs_signal_cache_lock:
                crypto_module._fs_signal_cache.clear()
                crypto_module._fs_signal_cache.update(previous_cache)
                crypto_module._fs_signal_scan_state.clear()
                crypto_module._fs_signal_scan_state.update(previous_state)

    def test_fs_observation_summary_uses_latest_batch_and_deduplicates_inventory_events(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        CryptoFsObservationLog.__table__.create(engine)
        now = datetime.now(timezone.utc)
        batches = [now - timedelta(minutes=15), now - timedelta(minutes=10), now - timedelta(minutes=5)]

        def observation(
            batch_time: datetime,
            futures_exchange: str,
            inventory_available: bool,
            *,
            symbol: str = "DUP",
        ) -> CryptoFsObservationLog:
            return CryptoFsObservationLog(
                batch_time=batch_time,
                symbol=symbol,
                futures_exchange=futures_exchange,
                spot_exchange="bg",
                daily_funding_rate=-0.02,
                potential_type="current_negative",
                inventory_available=inventory_available,
                executable_borrow=inventory_available,
                borrowable_amount=1000 if inventory_available else None,
                basis_rate=0.02,
                state="borrow_executable" if inventory_available else "watch_no_borrow",
                reason=None if inventory_available else "Bitget 支持借 DUP，但当前账户新增可借额度为 0",
                created_at=batch_time,
            )

        with Session(engine) as db:
            db.add(observation(now - timedelta(hours=1), "bn", True, symbol="STALE"))
            for futures_exchange in ("bn", "gt"):
                db.add(observation(batches[0], futures_exchange, True))
                db.add(observation(batches[1], futures_exchange, False))
                db.add(observation(batches[2], futures_exchange, True))
            db.commit()
            summary = fs_observation_summary(db, days=7)

        self.assertEqual(summary["currentCandidateCount"], 2)
        self.assertEqual(summary["currentBorrowableCount"], 2)
        self.assertEqual(summary["borrowLostCount"], 1)
        self.assertEqual(summary["borrowRecoveredCount"], 1)

    def test_kstr_monitor_only_marks_a_share_cash_sessions_open(self) -> None:
        self.assertTrue(a_share_cash_market_open(datetime(2026, 7, 21, 1, 30, tzinfo=timezone.utc)))
        self.assertFalse(a_share_cash_market_open(datetime(2026, 7, 21, 3, 31, tzinfo=timezone.utc)))
        self.assertTrue(a_share_cash_market_open(datetime(2026, 7, 21, 5, 0, tzinfo=timezone.utc)))

    def test_kstr_monitor_separates_open_auction_no_cancel_window(self) -> None:
        self.assertEqual(a_share_market_phase(datetime(2026, 7, 21, 1, 19, tzinfo=timezone.utc)), "open_auction_cancelable")
        self.assertEqual(a_share_market_phase(datetime(2026, 7, 21, 1, 20, tzinfo=timezone.utc)), "open_auction_no_cancel")
        self.assertEqual(a_share_market_phase(datetime(2026, 7, 21, 1, 25, tzinfo=timezone.utc)), "open_auction_matching")
        self.assertFalse(a_share_open_auction_observation_active(datetime(2026, 7, 21, 1, 15, tzinfo=timezone.utc)))
        self.assertFalse(a_share_open_auction_observation_active(datetime(2026, 7, 21, 1, 19, tzinfo=timezone.utc)))
        self.assertTrue(a_share_open_auction_observation_active(datetime(2026, 7, 21, 1, 20, tzinfo=timezone.utc)))
        self.assertTrue(a_share_open_auction_observation_active(datetime(2026, 7, 21, 1, 24, tzinfo=timezone.utc)))
        self.assertTrue(a_share_open_auction_observation_active(datetime(2026, 7, 21, 1, 25, tzinfo=timezone.utc)))
        self.assertFalse(a_share_open_auction_observation_active(datetime(2026, 7, 21, 1, 26, tzinfo=timezone.utc)))
        self.assertFalse(a_share_open_auction_observation_active(datetime(2026, 8, 1, 1, 25, tzinfo=timezone.utc)))
        self.assertFalse(a_share_cash_market_open(datetime(2026, 7, 21, 1, 22, tzinfo=timezone.utc)))
        self.assertEqual(a_share_market_phase(datetime(2026, 7, 21, 3, 31, tzinfo=timezone.utc)), "lunch_break")
        self.assertEqual(a_share_market_phase(datetime(2026, 7, 21, 6, 58, tzinfo=timezone.utc)), "close_auction")

    def test_kstr_refresh_window_excludes_lunch_break(self) -> None:
        self.assertFalse(a_share_kstr_refresh_window_active(datetime(2026, 7, 21, 1, 19, 59, tzinfo=timezone.utc)))
        self.assertTrue(a_share_kstr_refresh_window_active(datetime(2026, 7, 21, 1, 20, tzinfo=timezone.utc)))
        self.assertTrue(a_share_kstr_refresh_window_active(datetime(2026, 7, 21, 3, 30, tzinfo=timezone.utc)))
        self.assertFalse(a_share_kstr_refresh_window_active(datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc)))
        self.assertTrue(a_share_kstr_refresh_window_active(datetime(2026, 7, 21, 5, 0, tzinfo=timezone.utc)))
        self.assertTrue(a_share_kstr_refresh_window_active(datetime(2026, 7, 21, 7, 0, 59, tzinfo=timezone.utc)))
        self.assertFalse(a_share_kstr_refresh_window_active(datetime(2026, 7, 21, 7, 1, tzinfo=timezone.utc)))
        self.assertFalse(a_share_kstr_refresh_window_active(datetime(2026, 7, 25, 2, 0, tzinfo=timezone.utc)))

    def test_kstr_page_keeps_snapshot_without_background_refresh_outside_window(self) -> None:
        snapshot = {
            "status": "ok",
            "updatedAt": "2026-07-21T07:00:00Z",
            "executionReady": True,
            "aMarketOpen": True,
            "aMarketPhase": "continuous",
            "sessionComparison": {
                "isSynchronizedNow": True,
                "spreadFrozen": False,
            },
            "auctionWindow": {"isActive": False},
            "items": [],
        }
        with (
            patch("app.crypto.load_kstr_page_snapshot", return_value=snapshot),
            patch("app.crypto.start_kstr_page_snapshot_refresh") as refresh,
        ):
            refresh.return_value = False
            result = crypto_kstr_a_share_spread_page(
                now=datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc),
            )

        refresh.assert_called_once()
        self.assertFalse(result["diagnostics"]["autoRefreshActive"])
        self.assertFalse(result["diagnostics"]["backgroundRefreshStarted"])
        self.assertFalse(result["executionReady"])
        self.assertTrue(result["sessionComparison"]["spreadFrozen"])
        self.assertEqual(result["signalState"], "非刷新时段·保留最后结果")

    def test_kstr_background_refresh_does_not_start_outside_window(self) -> None:
        with patch("app.crypto.crypto_kstr_a_share_spread") as live_refresh:
            started = start_kstr_page_snapshot_refresh(
                "588000",
                "bn",
                now=datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc),
            )

        self.assertFalse(started)
        live_refresh.assert_not_called()

    def test_kstr_session_comparison_freezes_spread_and_separates_single_leg_move(self) -> None:
        synchronized_time = datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc)
        items = [
            {
                "time": synchronized_time,
                "spreadPct": 2.0,
                "kstrPriceUsdt": 25.0,
                "aEtfPriceCny": 1.9,
                "usdCny": 7.0,
                "source": "history",
                "marketPhase": "continuous",
            },
            {
                "time": synchronized_time + timedelta(minutes=1),
                "spreadPct": 2.4,
                "kstrPriceUsdt": 25.1,
                "aEtfPriceCny": 1.9,
                "usdCny": 7.0,
                "source": "auction",
                "marketPhase": "open_auction_no_cancel",
            },
        ]
        self.assertTrue(is_synchronized_kstr_spread_point(items[0]))
        self.assertFalse(is_synchronized_kstr_spread_point(items[1]))
        result = kstr_session_comparison(
            items,
            kstr_bid1=25.5,
            usd_cny=7.07,
            indicative_spread_pct=4.5,
            history_mean_pct=1.0,
            history_std_pct=0.5,
            execution_ready=False,
            reference_time=synchronized_time + timedelta(hours=2),
        )
        self.assertTrue(result["spreadFrozen"])
        self.assertEqual(result["lastSynchronizedTime"], synchronized_time)
        self.assertAlmostEqual(result["lastSynchronizedSpreadPct"], 2.0)
        self.assertAlmostEqual(result["lastSynchronizedZScore"], 2.0)
        self.assertAlmostEqual(result["kstrMoveSinceSynchronizedPct"], 2.0)
        self.assertAlmostEqual(result["fxMoveSinceSynchronizedPct"], 1.0)
        self.assertAlmostEqual(result["indicativeDriftSinceSynchronizedPct"], 2.5)

    def test_kstr_history_percentile_interpolates(self) -> None:
        self.assertEqual(percentile([0.0, 10.0], 0.5), 5.0)
        self.assertEqual(percentile([3.0], 0.9), 3.0)

    def test_kstr_symmetric_normalized_spread_is_leg_swap_symmetric(self) -> None:
        positive = kstr_symmetric_normalized_spread_pct(1.1, 1.0)
        swapped = kstr_symmetric_normalized_spread_pct(1 / 1.1, 1.0)
        self.assertIsNotNone(positive)
        self.assertIsNotNone(swapped)
        self.assertAlmostEqual(positive or 0, 9.5238095238)
        self.assertAlmostEqual(swapped or 0, -(positive or 0))
        self.assertEqual(kstr_symmetric_normalized_spread_pct(1.0, 1.0), 0.0)
        self.assertIsNone(kstr_symmetric_normalized_spread_pct(0.0, 1.0))

    def test_kstr_bid1_spread_uses_both_legs_best_bid(self) -> None:
        values = kstr_bid1_spread_values(1.99, 7.0, 0.99, 14.0)
        self.assertIsNotNone(values)
        expected_ratio = 1.99 * 7.0 / 0.99
        self.assertAlmostEqual((values or {})["rawRatio"], expected_ratio)
        self.assertAlmostEqual(
            (values or {})["spreadPct"],
            kstr_symmetric_normalized_spread_pct(expected_ratio, 14.0) or 0,
        )

    def test_kstr_frozen_order_book_keeps_indicative_open_and_close_spreads(self) -> None:
        values = kstr_order_book_spread_values(
            1.99,
            2.01,
            7.0,
            0.99,
            1.01,
            14.0,
            execution_ready=False,
        )

        self.assertIsNone(values["openSpreadPct"])
        self.assertIsNone(values["closeSpreadPct"])
        self.assertAlmostEqual(
            values["indicativeOpenSpreadPct"] or 0,
            kstr_symmetric_normalized_spread_pct(
                1.99 * 7.0 / 1.01,
                14.0,
            )
            or 0,
        )
        self.assertAlmostEqual(
            values["indicativeCloseSpreadPct"] or 0,
            kstr_symmetric_normalized_spread_pct(
                2.01 * 7.0 / 0.99,
                14.0,
            )
            or 0,
        )

    def test_kstr_live_order_book_promotes_indicative_spreads_to_executable(self) -> None:
        values = kstr_order_book_spread_values(
            1.99,
            2.01,
            7.0,
            0.99,
            1.01,
            14.0,
            execution_ready=True,
        )

        self.assertEqual(
            values["openSpreadPct"],
            values["indicativeOpenSpreadPct"],
        )
        self.assertEqual(
            values["closeSpreadPct"],
            values["indicativeCloseSpreadPct"],
        )

    def test_kstr_page_snapshot_persists_json_ready_last_success(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "588000.json"
            saved = save_kstr_page_snapshot(
                "588000",
                {
                    "status": "ok",
                    "updatedAt": datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc),
                    "items": [{"time": datetime(2026, 7, 24, 1, 59, tzinfo=timezone.utc)}],
                },
                path=path,
                force_disk=True,
            )
            self.assertEqual(saved["updatedAt"], "2026-07-24T02:00:00+00:00")
            loaded = load_kstr_page_snapshot("588000", path=path)
            self.assertIsNotNone(loaded)
            self.assertEqual(
                (loaded or {})["items"][0]["time"],
                "2026-07-24T01:59:00+00:00",
            )

    def test_kstr_legacy_multi_select_snapshot_loads_as_fixed_pair(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "588000.json"
            path.write_text(
                """
                {
                  "status": "ok",
                  "updatedAt": "2026-07-24T02:00:00Z",
                  "items": [],
                  "aEtfOptions": [
                    {"code": "588000", "priceClosest": false},
                    {"code": "588940", "priceClosest": true}
                  ],
                  "contractOptions": [
                    {"exchange": "bn", "selected": false},
                    {"exchange": "by", "selected": true},
                    {"exchange": "gt", "selected": false}
                  ],
                  "priceClosestAEtfCode": "588940",
                  "executionDefaultAEtfCode": "588940",
                  "liquidityFilter": {
                    "excludedCount": 2,
                    "excludedCodes": ["588180", "588940"]
                  },
                  "kstr": {"exchange": "bn"}
                }
                """,
                encoding="utf-8",
            )
            loaded = load_kstr_page_snapshot("588000", "bn", path=path)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(
            [row["code"] for row in loaded["aEtfOptions"]],
            ["588000"],
        )
        self.assertTrue(loaded["aEtfOptions"][0]["priceClosest"])
        self.assertTrue(loaded["aEtfOptions"][0]["executionDefault"])
        self.assertEqual(
            [row["exchange"] for row in loaded["contractOptions"]],
            ["bn"],
        )
        self.assertTrue(loaded["contractOptions"][0]["selected"])
        self.assertEqual(loaded["priceClosestAEtfCode"], "588000")
        self.assertEqual(loaded["executionDefaultAEtfCode"], "588000")
        self.assertEqual(
            loaded["liquidityFilter"],
            {
                "excludedCount": 0,
                "excludedCodes": [],
            },
        )

    def test_kstr_auction_archive_keeps_each_ten_second_bucket(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "588000-bn.jsonl"
            minute = datetime(2026, 7, 21, 1, 22, tzinfo=timezone.utc)
            base_item = {
                "time": minute,
                "aEtfPriceCny": 1.0,
                "kstrPriceUsdt": 2.0,
                "usdCny": 7.0,
                "rawRatio": 14.0,
                "spreadPct": 1.0,
                "marketPhase": "open_auction_no_cancel",
            }
            record_kstr_auction_tick(
                "588000",
                "bn",
                {
                    **base_item,
                    "time": minute + timedelta(seconds=10),
                    "observedAt": minute + timedelta(seconds=10),
                },
                path=path,
            )
            record_kstr_auction_tick(
                "588000",
                "bn",
                {
                    **base_item,
                    "time": minute + timedelta(seconds=19),
                    "observedAt": minute + timedelta(seconds=19),
                    "spreadPct": 1.2,
                },
                path=path,
            )
            record_kstr_auction_tick(
                "588000",
                "bn",
                {
                    **base_item,
                    "time": minute + timedelta(seconds=50),
                    "observedAt": minute + timedelta(seconds=50),
                    "spreadPct": 1.5,
                },
                path=path,
            )
            loaded = load_kstr_auction_ticks(
                "588000",
                "bn",
                path=path,
                now=minute + timedelta(minutes=1),
            )
            self.assertEqual(len(loaded), 2)
            self.assertEqual(
                [row["time"].second for row in loaded],
                [10, 50],
            )
            self.assertEqual(loaded[0]["observedAt"], minute + timedelta(seconds=19))
            self.assertEqual(loaded[0]["spreadPct"], 1.2)
            self.assertEqual(loaded[1]["spreadPct"], 1.5)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_kstr_auction_chart_only_loads_0920_to_0925(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "588000-bn.jsonl"
            base_item = {
                "aEtfPriceCny": 1.0,
                "kstrPriceUsdt": 2.0,
                "usdCny": 7.0,
                "rawRatio": 14.0,
                "spreadPct": 1.0,
                "marketPhase": "open_auction_no_cancel",
            }
            for minute in (19, 20, 25, 26):
                timestamp = datetime(2026, 7, 21, 1, minute, tzinfo=timezone.utc)
                record_kstr_auction_tick(
                    "588000",
                    "bn",
                    {
                        **base_item,
                        "time": timestamp,
                        "observedAt": timestamp,
                    },
                    path=path,
                )
            for second in (10, 50):
                timestamp = datetime(
                    2026,
                    7,
                    21,
                    1,
                    25,
                    second,
                    tzinfo=timezone.utc,
                )
                record_kstr_auction_tick(
                    "588000",
                    "bn",
                    {
                        **base_item,
                        "time": timestamp,
                        "observedAt": timestamp,
                        "spreadPct": 2.0 + second / 100,
                    },
                    path=path,
                )
            loaded = load_kstr_auction_ticks(
                "588000",
                "bn",
                path=path,
                now=datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(
                [row["time"].minute for row in loaded],
                [20, 25],
            )
            self.assertEqual([row["time"].second for row in loaded], [0, 0])
            self.assertEqual(loaded[-1]["spreadPct"], 1.0)

    def test_kstr_snapshot_merge_replaces_stale_auction_rows_with_normalized_ticks(
        self,
    ) -> None:
        final_time = datetime(2026, 7, 30, 1, 25, tzinfo=timezone.utc)
        final_tick = {
            "time": final_time,
            "observedAt": final_time,
            "aEtfPriceCny": 1.75,
            "kstrPriceUsdt": 24.0,
            "usdCny": 7.1,
            "rawRatio": 97.371428,
            "spreadPct": 1.0,
            "source": "auction",
            "marketPhase": "open_auction_matching",
        }
        payload = {
            "status": "ok",
            "auctionWindow": {},
            "items": [
                {
                    **final_tick,
                    "time": "2026-07-30T01:25:10Z",
                    "observedAt": "2026-07-30T01:25:10Z",
                },
                {
                    **final_tick,
                    "time": "2026-07-30T01:25:40Z",
                    "observedAt": "2026-07-30T01:25:40Z",
                },
                {
                    "time": "2026-07-30T01:30:10Z",
                    "source": "live",
                    "marketPhase": "continuous",
                    "spreadPct": 1.2,
                },
            ],
        }
        with (
            patch(
                "app.crypto.load_kstr_auction_ticks",
                return_value=[final_tick],
            ),
            patch(
                "app.crypto.load_kstr_auction_backfill_audit",
                return_value=None,
            ),
        ):
            result = crypto_module.merge_kstr_auction_ticks_into_payload(
                payload,
                "588000",
                "bn",
            )

        auction_rows = [
            row for row in result["items"] if row.get("source") == "auction"
        ]
        self.assertEqual(len(auction_rows), 1)
        self.assertEqual(auction_rows[0]["time"], final_time.isoformat())
        self.assertEqual(
            len([row for row in result["items"] if row.get("source") == "live"]),
            1,
        )

    def test_kstr_continuous_tick_cache_keeps_ten_second_bid1_points(self) -> None:
        cache_key = ("588000", "bn")
        with crypto_module._kstr_continuous_ticks_lock:
            previous = crypto_module._kstr_continuous_ticks.pop(cache_key, None)
        base_time = datetime(2026, 7, 30, 1, 30, tzinfo=timezone.utc)
        base_item = {
            "aEtfPriceCny": 1.75,
            "kstrPriceUsdt": 24.0,
            "usdCny": 7.1,
            "rawRatio": 97.371428,
            "spreadPct": 1.0,
            "marketPhase": "continuous",
            "priceMode": "both_legs_bid1",
        }
        try:
            crypto_module.record_kstr_continuous_tick(
                "588000",
                "bn",
                {
                    **base_item,
                    "time": base_time + timedelta(seconds=3),
                    "observedAt": base_time + timedelta(seconds=3),
                },
            )
            crypto_module.record_kstr_continuous_tick(
                "588000",
                "bn",
                {
                    **base_item,
                    "time": base_time + timedelta(seconds=8),
                    "observedAt": base_time + timedelta(seconds=8),
                    "spreadPct": 1.1,
                },
            )
            crypto_module.record_kstr_continuous_tick(
                "588000",
                "bn",
                {
                    **base_item,
                    "time": base_time + timedelta(seconds=13),
                    "observedAt": base_time + timedelta(seconds=13),
                    "spreadPct": 1.2,
                },
            )

            loaded = crypto_module.load_kstr_continuous_ticks(
                "588000",
                "bn",
                now=base_time + timedelta(minutes=1),
            )

            self.assertEqual([row["time"].second for row in loaded], [0, 10])
            self.assertEqual([row["spreadPct"] for row in loaded], [1.1, 1.2])
            self.assertTrue(all(row["source"] == "live" for row in loaded))
            self.assertTrue(
                all(row["priceMode"] == "both_legs_bid1" for row in loaded)
            )
        finally:
            with crypto_module._kstr_continuous_ticks_lock:
                if previous is None:
                    crypto_module._kstr_continuous_ticks.pop(cache_key, None)
                else:
                    crypto_module._kstr_continuous_ticks[cache_key] = previous

    def test_kstr_auction_backfill_audit_uses_fixed_pair(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "588000-bn-audit.json"
            path.write_text(
                """
                {
                  "aEtfCode": "588000",
                  "contractExchange": "bn",
                  "tradeDate": "2026-07-28",
                  "status": "partial"
                }
                """,
                encoding="utf-8",
            )
            loaded = load_kstr_auction_backfill_audit(
                "588000",
                "bn",
                path=path,
            )
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["tradeDate"], "2026-07-28")
            self.assertEqual(loaded["aEtfCode"], "588000")
            self.assertEqual(loaded["contractExchange"], "bn")

    def test_kstr_auction_capture_collects_binance_final_match(self) -> None:
        now = datetime(2026, 7, 21, 1, 25, 40, tzinfo=timezone.utc)

        def contract_snapshot(_client, exchange):
            return {
                "exchange": exchange,
                "bid": 1.99,
                "ask": 2.01,
                "mid": 2.0,
                "referenceTime": now,
            }

        def record_tick(code, exchange, item):
            return {
                **item,
                "aEtfCode": code,
                "contractExchange": exchange,
            }

        with (
            patch(
                "app.crypto.load_kstr_page_snapshot",
                return_value={"referenceRatio": 14.0},
            ),
            patch("app.crypto.http_client", return_value=nullcontext(object())),
            patch(
                "app.crypto.tencent_a_share_quote",
                return_value={
                    "price": 1.0,
                    "bid": 0.99,
                    "ask": 1.01,
                    "referenceTime": now,
                    "sourceName": "test",
                },
            ),
            patch(
                "app.crypto.usd_cny_quote",
                return_value=(7.0, now, "test-fx", "https://example.com"),
            ),
            patch(
                "app.crypto.kstr_contract_market_snapshot",
                side_effect=contract_snapshot,
            ),
            patch(
                "app.crypto.record_kstr_auction_tick",
                side_effect=record_tick,
            ) as archive,
        ):
            result = capture_kstr_auction_observations(
                now=now,
                a_etf_code="588000",
                contract_exchanges=("bn",),
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [item["contractExchange"] for item in result["captured"]],
            ["bn"],
        )
        self.assertTrue(all(item["isFinalMatchPoint"] for item in result["captured"]))
        self.assertTrue(all(item["kstrPriceUsdt"] == 1.99 for item in result["captured"]))
        self.assertTrue(all(item["aEtfPriceCny"] == 0.99 for item in result["captured"]))
        self.assertTrue(all(item["priceMode"] == "both_legs_bid1" for item in result["captured"]))
        archive.assert_called_once()

    def test_kstr_etf_selector_accepts_only_fixed_etf(self) -> None:
        self.assertEqual(normalize_kstr_a_etf_code("sh588000"), "588000")
        self.assertEqual(normalize_kstr_a_etf_code("588000.SH"), "588000")
        with self.assertRaisesRegex(ValueError, "不支持的科创50ETF"):
            normalize_kstr_a_etf_code("588940.SH")

    def test_kstr_contract_selector_accepts_only_binance(self) -> None:
        self.assertEqual(normalize_kstr_contract_exchange("binance"), "bn")
        self.assertEqual(normalize_kstr_contract_exchange("BN"), "bn")
        with self.assertRaises(ValueError):
            normalize_kstr_contract_exchange("BYBIT")
        with self.assertRaises(ValueError):
            normalize_kstr_contract_exchange("Gate")

    @patch("app.crypto.request_json")
    def test_kstr_binance_contract_snapshot_combines_book_and_premium_index(
        self,
        request_json,
    ) -> None:
        request_json.side_effect = [
            {
                "symbol": "KSTRUSDT",
                "bidPrice": "24.94",
                "askPrice": "24.99",
                "time": 1_785_215_766_000,
            },
            {
                "symbol": "KSTRUSDT",
                "markPrice": "24.96",
                "indexPrice": "24.93",
                "lastFundingRate": "-0.0001",
                "nextFundingTime": 1_785_225_600_000,
            },
        ]
        result = kstr_contract_market_snapshot(object(), "bn")
        self.assertEqual(result["exchange"], "bn")
        self.assertEqual(result["symbol"], "KSTRUSDT")
        self.assertEqual(result["sourceName"], "Binance USDⓈ-M Futures")
        self.assertEqual(result["bid"], 24.94)
        self.assertEqual(result["ask"], 24.99)
        self.assertEqual(result["mid"], 24.965)
        self.assertEqual(result["markPrice"], 24.96)
        self.assertEqual(result["indexPrice"], 24.93)
        self.assertEqual(result["fundingRate"], -0.0001)
        self.assertEqual(
            result["nextFundingTime"],
            datetime.fromtimestamp(1_785_225_600, tz=timezone.utc),
        )
        self.assertEqual(request_json.call_count, 2)
        self.assertIn(
            "/fapi/v1/ticker/bookTicker",
            request_json.call_args_list[0].args[1],
        )
        self.assertIn(
            "/fapi/v1/premiumIndex",
            request_json.call_args_list[1].args[1],
        )

    def test_kstr_etf_selector_keeps_fixed_etf_when_liquid(self) -> None:
        eligible, excluded = kstr_liquid_etf_options(
            [
                {"code": "588000", "dayTurnoverCny": 2_000_000_000},
            ]
        )
        self.assertEqual([row["code"] for row in eligible], ["588000"])
        self.assertEqual(excluded, [])

    def test_kstr_etf_selector_keeps_fixed_etf_when_quote_is_unavailable(self) -> None:
        eligible, excluded = kstr_liquid_etf_options(
            [
                {"code": "588000", "dayTurnoverCny": None},
            ]
        )
        self.assertEqual([row["code"] for row in eligible], ["588000"])
        self.assertEqual(excluded, [])

    def test_kstr_etf_similarity_uses_normalized_common_session_prices(self) -> None:
        start = datetime(2026, 7, 1, 1, 30, tzinfo=timezone.utc)
        a_rows = {}
        kstr_rows = []
        for index in range(31):
            timestamp = int((start + timedelta(minutes=index * 5)).timestamp() * 1000)
            a_price = 1.8 * (1 + index * 0.001)
            a_rows[timestamp] = a_price
            kstr_rows.append({"ts": timestamp, "close": a_price * 2})
        metrics = kstr_etf_similarity_metrics(a_rows, kstr_rows, {"2026-07-01": 7.0})
        self.assertEqual(metrics["sampleCount"], 31)
        self.assertAlmostEqual(metrics["residualStdPct"], 0.0)
        self.assertAlmostEqual(metrics["returnCorrelation"], 1.0)

    def test_kstr_beta_stays_at_one_until_twenty_daily_returns_exist(self) -> None:
        rows = []
        a_price = 1.8
        kstr_price = 1.8
        for index in range(13):
            daily_return = (0.002 + index * 0.0002) if index % 2 == 0 else (-0.001 - index * 0.0001)
            a_price *= 1 + daily_return
            kstr_price *= 1 + daily_return * 1.1
            rows.append(
                {
                    "time": datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc) + timedelta(days=index),
                    "aEtfPriceCny": a_price,
                    "kstrEquivalentPriceCny": kstr_price,
                }
            )
        beta, raw_beta, sample_count, status = synchronized_return_beta(rows)
        self.assertEqual(beta, 1.0)
        self.assertEqual(sample_count, 12)
        self.assertIsNotNone(raw_beta)
        self.assertIn("暂不启用校正", status)

    def test_kstr_structural_beta_uses_synchronized_return_pairs(self) -> None:
        pairs = [
            (-0.02, -0.021),
            (-0.01, -0.009),
            (0.005, 0.006),
            (0.01, 0.011),
            (0.02, 0.019),
            (0.03, 0.031),
        ]
        stats = single_factor_return_stats(pairs)
        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertGreater(stats["correlation"], 0.99)
        self.assertAlmostEqual(stats["beta"], 1.0, delta=0.1)
        self.assertEqual(stats["sampleCount"], len(pairs))

    def test_fs_display_keeps_all_negative_potential_rows_and_marks_borrow_priority(self) -> None:
        def signal(
            symbol: str,
            status: str,
            can_borrow: bool | None,
            inventory_available: bool | None = None,
            amount: float | None = None,
            value: float | None = None,
            basis_rate: float | None = None,
            message: str | None = None,
        ) -> dict:
            return {
                "signalKey": symbol,
                "symbol": symbol,
                "spotExchange": "bg",
                "checks": {
                    "bg": {
                        "exchange": "bg",
                        "status": status,
                        "message": message,
                        "canBorrow": can_borrow,
                        "inventoryAvailable": inventory_available,
                        "borrowableAmount": amount,
                        "borrowableValueUsdt": value,
                    }
                },
                "actionable": False,
                "watchOnly": True,
                "pushed": False,
                "currentFundingRate": -0.001,
                "premiumRate": -0.002,
                "basisRate": basis_rate,
            }

        payload = normalize_fs_signals_payload(
            {
                "items": [
                    signal("AVAILABLE", "ok", True, True, 500, 500),
                    signal("SMALL", "not_borrowable", False, True, 5, 25),
                    signal(
                        "LARGE_NO_B",
                        "not_borrowable",
                        False,
                        False,
                        basis_rate=-0.05,
                        message="Bitget 支持借 LARGE_NO_B，但当前账户新增可借额度为 0",
                    ),
                    signal("UNAVAILABLE", "not_borrowable", False, False),
                    signal("UNVERIFIED", "error", None, None, message="HTTP 400: 25112 当前币种未开启抵押"),
                ]
            }
        )
        self.assertEqual(
            [item["symbol"] for item in payload["items"]],
            ["AVAILABLE", "SMALL", "LARGE_NO_B"],
        )
        self.assertTrue(payload["items"][2]["limitOrderCandidate"])
        self.assertEqual(payload["items"][2]["watchReason"], "大差价 · 可挂单等待 B")
        self.assertEqual(payload["watchCount"], 3)
        self.assertEqual(payload["borrowableCount"], 2)
        self.assertEqual(payload["largeSpreadNoBorrowCount"], 1)
        self.assertEqual(payload["hiddenHardUnavailableCount"], 2)
        self.assertEqual(payload["hiddenUnavailableCount"], 0)

    def test_fs_display_includes_binance_as_a_borrowable_platform(self) -> None:
        payload = normalize_fs_signals_payload(
            {
                "items": [
                    {
                        "signalKey": "BN_AVAILABLE",
                        "symbol": "BN_AVAILABLE",
                        "spotExchange": "bn",
                        "checks": {
                            "bg": {
                                "exchange": "bg",
                                "status": "not_supported",
                                "message": "Bitget 当前不支持借 BN_AVAILABLE",
                                "canBorrow": False,
                                "inventoryAvailable": False,
                            },
                            "bn": {
                                "exchange": "bn",
                                "status": "ok",
                                "message": "Binance 可借 500 BN_AVAILABLE",
                                "canBorrow": True,
                                "inventoryAvailable": True,
                                "borrowableAmount": 500,
                                "borrowableValueUsdt": 500,
                            },
                        },
                        "actionable": False,
                        "watchOnly": True,
                        "pushed": False,
                        "currentFundingRate": -0.001,
                        "premiumRate": -0.002,
                        "basisRate": -0.01,
                    }
                ]
            }
        )

        self.assertEqual([item["symbol"] for item in payload["items"]], ["BN_AVAILABLE"])
        self.assertTrue(payload["items"][0]["inventoryAvailable"])
        self.assertTrue(payload["items"][0]["executableBorrow"])
        self.assertEqual(payload["borrowableCount"], 1)
        self.assertEqual(payload["hiddenHardUnavailableCount"], 0)

    def test_fs_display_uses_astro_open_and_close_spreads_from_selected_platform(self) -> None:
        payload = normalize_fs_signals_payload(
            {
                "items": [
                    {
                        "signalKey": "ASTRO_SPREAD",
                        "symbol": "ASTRO_SPREAD",
                        "spotExchange": "bn",
                        "checks": {
                            "bn": {
                                "exchange": "bn",
                                "status": "ok",
                                "message": "Binance 可借",
                                "canBorrow": True,
                                "inventoryAvailable": True,
                                "borrowableAmount": 500,
                                "borrowableValueUsdt": 500,
                                "openSpreadRate": 0.0035,
                                "closeSpreadRate": 0.0031,
                            }
                        },
                        "actionable": False,
                        "watchOnly": True,
                        "pushed": False,
                        "currentFundingRate": -0.001,
                        "premiumRate": -0.002,
                    }
                ]
            }
        )

        signal = payload["items"][0]
        self.assertEqual(signal["openSpreadRate"], 0.0035)
        self.assertEqual(signal["closeSpreadRate"], 0.0031)
        self.assertEqual(signal["spreadRate"], 0.0035)

    def test_fs_large_spread_survives_temporary_borrow_api_error(self) -> None:
        payload = normalize_fs_signals_payload(
            {
                "items": [
                    {
                        "signalKey": "RIF",
                        "symbol": "RIF",
                        "spotExchange": "bg",
                        "checks": {
                            "bg": {
                                "exchange": "bg",
                                "status": "error",
                                "message": "杠杆可借检查超时",
                                "canBorrow": False,
                                "openSpreadRate": 0.0444,
                                "closeSpreadRate": 0.0461,
                            }
                        },
                        "actionable": False,
                        "watchOnly": True,
                        "pushed": False,
                        "currentFundingRate": -0.0055,
                        "premiumRate": -0.04,
                    }
                ]
            }
        )

        self.assertEqual([item["symbol"] for item in payload["items"]], ["RIF"])
        self.assertTrue(payload["items"][0]["borrowRouteUnknown"])
        self.assertTrue(payload["items"][0]["limitOrderCandidate"])

    def test_fs_candidate_accepts_negative_premium_before_funding_turns_negative(self) -> None:
        candidates: list[dict] = []
        append_negative_candidate(
            candidates,
            {
                "exchange": "bn",
                "symbol": "EARLY",
                "fundingRate": 0.0001,
                "premiumRate": -0.0002,
                "periodHours": 8,
            },
        )
        append_negative_candidate(
            candidates,
            {
                "exchange": "bn",
                "symbol": "POSITIVE",
                "fundingRate": 0.0001,
                "premiumRate": 0.0002,
                "periodHours": 8,
            },
        )
        self.assertEqual([item["symbol"] for item in candidates], ["EARLY"])
        self.assertEqual(candidates[0]["potentialType"], "premium_negative")

    @patch("app.crypto.signed_bitget_uta_post")
    @patch("app.crypto.request_json")
    def test_bitget_uta_requires_positive_incremental_borrow_inventory(
        self,
        request_json_mock,
        signed_post_mock,
    ) -> None:
        request_json_mock.return_value = {"data": {"dailyInterest": "0.01"}}
        signed_post_mock.return_value = {
            "data": {
                "available": "20",
                "maxOpen": "20",
            }
        }
        check = fetch_bitget_uta_margin_short_check(object(), "O")
        self.assertFalse(check.can_borrow)
        self.assertEqual(check.status, "not_borrowable")
        self.assertIsNone(check.borrowable_amount)
        self.assertIn("新增可借为 0", check.message)

    @patch("app.crypto.signed_bitget_uta_post")
    @patch("app.crypto.request_json")
    def test_bitget_uta_subtracts_existing_balance_from_max_open(
        self,
        request_json_mock,
        signed_post_mock,
    ) -> None:
        request_json_mock.return_value = {"data": {"dailyInterest": "0.01"}}
        signed_post_mock.return_value = {
            "data": {
                "available": "20",
                "maxOpen": "120",
            }
        }
        check = fetch_bitget_uta_margin_short_check(object(), "O")
        self.assertTrue(check.can_borrow)
        self.assertEqual(check.status, "ok")
        self.assertEqual(check.borrowable_amount, 100)

    @patch("app.crypto.signed_bitget_uta_post")
    @patch("app.crypto.request_json")
    def test_bitget_uta_keeps_reference_rate_when_collateral_is_disabled(
        self,
        request_json_mock,
        signed_post_mock,
    ) -> None:
        request_json_mock.return_value = {"data": {"dailyInterest": "0.008"}}
        signed_post_mock.side_effect = ValueError("HTTP 400: 25112 当前币种未开启抵押")
        check = fetch_bitget_uta_margin_short_check(object(), "TLM")
        self.assertEqual(check.status, "not_borrowable")
        self.assertFalse(check.can_borrow)
        self.assertAlmostEqual(check.hourly_borrow_rate or 0, 0.008 / 24)
        self.assertEqual(check.daily_borrow_rate, 0.008)
        self.assertEqual(check.message, "Bitget 当前未开启 TLM 抵押，暂时无 B")

    @patch("app.crypto.request_json")
    @patch("app.crypto.signed_bitget_uta_post")
    def test_bitget_fast_scan_uses_incremental_inventory_without_rate_request(
        self,
        signed_post_mock,
        request_json_mock,
    ) -> None:
        request_json_mock.return_value = {
            "data": {"dailyInterest": "0.008", "platformRemaingQuota": "1000"}
        }
        signed_post_mock.return_value = {
            "data": {
                "available": "20",
                "maxOpen": "120",
            }
        }

        check = fetch_bitget_fs_fast_inventory_check(
            object(),
            "O",
            {
                "spotBid": 2.0,
                "spotAsk": 2.1,
                "dailyBorrowRate": 0.008,
            },
        )

        self.assertTrue(check["canBorrow"])
        self.assertTrue(check["inventoryAvailable"])
        self.assertEqual(check["borrowableAmount"], 100)
        self.assertEqual(check["dailyBorrowRate"], 0.008)

    @patch("app.crypto.request_json")
    @patch("app.crypto.signed_bitget_uta_post")
    def test_bitget_fast_scan_caps_max_open_by_platform_quota(
        self,
        signed_post_mock,
        request_json_mock,
    ) -> None:
        request_json_mock.return_value = {
            "data": {"dailyInterest": "0.0032592", "platformRemaingQuota": "35.37960286"}
        }
        signed_post_mock.return_value = {
            "data": {
                "available": "0",
                "maxOpen": "4367.2",
            }
        }

        check = fetch_bitget_fs_fast_inventory_check(
            object(),
            "MMT",
            {
                "spotBid": 0.1789,
                "spotAsk": 0.1791,
                "dailyBorrowRate": 0.0032592,
            },
        )

        self.assertFalse(check["canBorrow"])
        self.assertFalse(check["inventoryAvailable"])
        self.assertEqual(check["status"], "not_borrowable")
        self.assertAlmostEqual(check["borrowableValueUsdt"], 6.329410951654)
        self.assertIn("未超过 100U", check["message"])
        self.assertIn("暂无可用 B", check["message"])

    def test_binance_fast_scan_keeps_small_inventory_visible(self) -> None:
        check = fs_binance_fast_inventory_check(
            "TLM",
            {"TLM": 10},
            {
                "spotBid": 5.0,
                "spotAsk": 5.0,
                "dailyBorrowRate": 0.01,
            },
        )

        self.assertTrue(check["inventoryAvailable"])
        self.assertFalse(check["canBorrow"])
        self.assertEqual(check["borrowableAmount"], 10)
        self.assertEqual(check["borrowableValueUsdt"], 50)

    def test_fast_scan_error_does_not_turn_previous_b_into_no_b(self) -> None:
        signal = {
            "symbol": "RIF",
            "currentFundingRate": -0.003,
            "periodHours": 1,
            "feeRate": 0.0008,
            "slippageRate": 0.0005,
            "checks": {
                "bg": {
                    "exchange": "bg",
                    "status": "ok",
                    "canBorrow": True,
                    "inventoryAvailable": True,
                    "borrowableAmount": 500,
                    "dailyBorrowRate": 0.01,
                }
            },
        }

        updated = merge_fs_fast_borrow_checks(
            signal,
            {
                "bg": {
                    "exchange": "bg",
                    "status": "error",
                    "message": "HTTP 429",
                    "canBorrow": None,
                    "inventoryAvailable": None,
                }
            },
        )

        self.assertTrue(updated["checks"]["bg"]["inventoryAvailable"])
        self.assertTrue(updated["checks"]["bg"]["canBorrow"])
        self.assertTrue(updated["checks"]["bg"]["staleInventory"])

    def test_fast_market_refresh_updates_funding_period_and_dependent_values(self) -> None:
        next_funding = datetime(2026, 8, 8, 4, 0, tzinfo=timezone.utc)
        signal = {
            "signalKey": "old",
            "symbol": "VANRY",
            "futuresExchange": "gt",
            "spotExchange": "bg",
            "currentFundingRate": -0.001,
            "periodHours": 8,
            "feeRate": 0.0008,
            "slippageRate": 0.0005,
            "actionable": False,
            "watchOnly": True,
            "checks": {
                "bg": {
                    "exchange": "bg",
                    "status": "ok",
                    "canBorrow": True,
                    "inventoryAvailable": True,
                    "spotBid": 0.0033,
                    "spotAsk": 0.00331,
                    "borrowPeriodRate": 0.00001,
                }
            },
        }
        quote = MarketQuote(
            exchange="gt",
            symbol="VANRY",
            market_type="futures",
            best_bid=0.0032,
            best_ask=0.00321,
            funding_rate=-0.005,
            premium_rate=-0.0049,
            next_funding_time=next_funding,
            period_hours=4,
            status="ok",
            updated_at=datetime.now(timezone.utc),
        )

        updated = merge_fs_fast_market_quote(signal, quote)

        self.assertEqual(updated["currentFundingRate"], -0.005)
        self.assertEqual(updated["dailyFundingRate"], -0.03)
        self.assertEqual(updated["periodHours"], 4)
        self.assertEqual(updated["fundingTime"], next_funding)
        self.assertFalse(updated["fundingStale"])
        self.assertAlmostEqual(updated["openSpreadRate"], 2 * (0.0033 - 0.00321) / (0.0033 + 0.00321))
        self.assertAlmostEqual(updated["closeSpreadRate"], 2 * (0.00331 - 0.0032) / (0.00331 + 0.0032))
        self.assertEqual(updated["checks"]["bg"]["netFundingRate"], updated["netFundingRate"])

    def test_fast_market_refresh_preserves_last_value_when_source_fails(self) -> None:
        signal = {
            "symbol": "VANRY",
            "futuresExchange": "gt",
            "currentFundingRate": -0.005,
            "periodHours": 4,
        }
        quote = MarketQuote(
            exchange="gt",
            symbol="VANRY",
            market_type="futures",
            status="error",
            error="HTTP 429",
        )

        updated = merge_fs_fast_market_quote(signal, quote)

        self.assertEqual(updated["currentFundingRate"], -0.005)
        self.assertTrue(updated["fundingStale"])
        self.assertEqual(updated["fundingRefreshError"], "HTTP 429")

    def test_fast_scan_does_not_remove_unchecked_no_b_candidates(self) -> None:
        previous_cache = dict(crypto_module._fs_signal_cache)
        try:
            crypto_module._fs_signal_cache.clear()
            crypto_module._fs_signal_cache[20] = (
                datetime.now(timezone.utc),
                {
                    "status": "ok",
                    "items": [
                        {
                            "symbol": "DATA",
                            "futuresExchange": "by",
                            "currentFundingRate": -0.002,
                            "periodHours": 4,
                            "inventoryAvailable": True,
                            "watchOnly": True,
                            "checks": {
                                "bg": {
                                    "exchange": "bg",
                                    "status": "ok",
                                    "canBorrow": True,
                                    "inventoryAvailable": True,
                                    "borrowableAmount": 1000,
                                }
                            },
                        },
                        {
                            "symbol": "TLM",
                            "futuresExchange": "by",
                            "currentFundingRate": -0.01,
                            "periodHours": 4,
                            "inventoryAvailable": False,
                            "watchOnly": True,
                            "limitOrderCandidate": True,
                            "checks": {
                                "bg": {
                                    "exchange": "bg",
                                    "status": "error",
                                    "canBorrow": False,
                                    "inventoryAvailable": False,
                                    "message": "当前未开启抵押",
                                }
                            },
                        },
                    ],
                    "watchItems": [],
                },
            )

            update_fs_signal_caches_from_fast_scan(
                {
                    "DATA": {
                        "bg": {
                            "exchange": "bg",
                            "status": "ok",
                            "canBorrow": True,
                            "inventoryAvailable": True,
                            "borrowableAmount": 1200,
                        }
                    }
                },
                {},
                persist=False,
            )

            cached_items = crypto_module._fs_signal_cache[20][1]["items"]
            self.assertEqual([item["symbol"] for item in cached_items], ["DATA", "TLM"])
            self.assertEqual(crypto_module._fs_signal_cache[20][1]["potentialCount"], 2)
        finally:
            crypto_module._fs_signal_cache.clear()
            crypto_module._fs_signal_cache.update(previous_cache)

    @patch("app.crypto.fetch_binance_spot_usdt_symbols")
    @patch("app.crypto.fetch_binance_margin_available_inventory")
    @patch("app.crypto.fetch_binance_next_hourly_interest_rates")
    def test_binance_fs_check_uses_positive_bulk_inventory(
        self,
        hourly_rates_mock,
        inventory_mock,
        spot_symbols_mock,
    ) -> None:
        hourly_rates_mock.return_value = {"CAKE": 0.00013}
        spot_symbols_mock.return_value = {"CAKE"}
        inventory_mock.return_value = {"CAKE": 1200.0}

        check = fetch_binance_fs_margin_short_check(object(), "CAKE")

        self.assertTrue(check.can_borrow)
        self.assertTrue(check.inventory_available)
        self.assertEqual(check.borrowable_amount, 1200)
        self.assertEqual(check.hourly_borrow_rate, 0.00013)
        self.assertAlmostEqual(check.daily_borrow_rate, 0.00312)
        self.assertIn("下单前", check.message)

    @patch("app.crypto.fetch_binance_spot_usdt_symbols")
    @patch("app.crypto.fetch_binance_margin_available_inventory")
    @patch("app.crypto.fetch_binance_next_hourly_interest_rates")
    def test_binance_fs_check_rejects_zero_bulk_inventory(
        self,
        hourly_rates_mock,
        inventory_mock,
        spot_symbols_mock,
    ) -> None:
        spot_symbols_mock.return_value = {"TLM"}
        inventory_mock.return_value = {"TLM": 0.0}
        hourly_rates_mock.return_value = {"TLM": 0.0002}

        check = fetch_binance_fs_margin_short_check(object(), "TLM")

        self.assertFalse(check.can_borrow)
        self.assertFalse(check.inventory_available)
        self.assertEqual(check.status, "not_borrowable")
        self.assertEqual(check.hourly_borrow_rate, 0.0002)
        self.assertIn("库存为 0", check.message)

    @patch("app.crypto.signed_binance_margin_get")
    def test_binance_hourly_interest_rates_are_loaded_in_one_batch(self, signed_get_mock) -> None:
        signed_get_mock.return_value = [
            {"asset": "RATEFIXA", "nextHourlyInterestRate": "0.00001"},
            {"asset": "RATEFIXB", "nextHourlyInterestRate": "0.00002"},
        ]

        client = object()
        rates = fetch_binance_next_hourly_interest_rates(client, {"RATEFIXB", "RATEFIXA"})

        self.assertEqual(rates, {"RATEFIXA": 0.00001, "RATEFIXB": 0.00002})
        self.assertEqual(
            signed_get_mock.call_args.args,
            (
                client,
                "/sapi/v1/margin/next-hourly-interest-rate",
                {"assets": "RATEFIXA,RATEFIXB", "isIsolated": "false"},
            ),
        )

    @patch("app.crypto.signed_binance_margin_get")
    def test_binance_transfer_config_is_shared_across_symbols(self, signed_get_mock) -> None:
        signed_get_mock.return_value = [
            {
                "coin": "RIF",
                "networkList": [
                    {
                        "network": "RSK",
                        "depositEnable": True,
                        "withdrawEnable": False,
                        "withdrawFee": "1",
                        "withdrawMin": "2",
                    }
                ],
            },
            {
                "coin": "TLM",
                "networkList": [
                    {
                        "network": "BSC",
                        "depositEnable": True,
                        "withdrawEnable": True,
                    }
                ],
            },
        ]

        with patch("app.crypto._binance_transfer_config_cache", None):
            rif = fetch_binance_transfer_status(object(), "RIF")
            tlm = fetch_binance_transfer_status(object(), "TLM")

        self.assertTrue(rif.deposit_enabled)
        self.assertFalse(rif.withdraw_enabled)
        self.assertTrue(tlm.deposit_enabled)
        self.assertTrue(tlm.withdraw_enabled)
        self.assertEqual(signed_get_mock.call_count, 1)

    @patch("app.crypto.write_fs_borrow_cache")
    @patch("app.crypto.fetch_coin_transfer_status")
    @patch("app.crypto.fetch_binance_fs_margin_short_check")
    @patch("app.crypto.fetch_fs_market_quote")
    @patch("app.crypto.mapped_symbol_and_ratio_for")
    def test_fs_binance_check_includes_transfer_status(
        self,
        mapped_symbol_mock,
        market_quote_mock,
        borrow_check_mock,
        transfer_status_mock,
        _write_cache_mock,
    ) -> None:
        mapped_symbol_mock.return_value = ("RIF", 1.0)
        market_quote_mock.return_value = MarketQuote(
            exchange="bn",
            symbol="RIF",
            market_type="spot",
            status="ok",
            best_bid=0.08,
            best_ask=0.081,
        )
        borrow_check_mock.return_value = MarginShortCheck(
            exchange="bn",
            symbol="RIF",
            status="not_borrowable",
            message="当前库存为 0",
            can_borrow=False,
            inventory_available=False,
        )
        transfer_status_mock.return_value = CoinTransferStatus(
            exchange="bn",
            symbol="RIF",
            status="ok",
            message="Binance 充提状态已更新。",
            deposit_enabled=True,
            withdraw_enabled=False,
            chains=[],
        )

        check, candidate = evaluate_fs_spot_exchange(
            object(),
            "RIF",
            "gt",
            "bn",
            1,
            -0.003,
            0.078,
            0.079,
        )

        self.assertIsNone(candidate)
        self.assertEqual(check["transferStatus"]["exchange"], "bn")
        self.assertTrue(check["transferStatus"]["depositEnabled"])
        self.assertFalse(check["transferStatus"]["withdrawEnabled"])

    def test_fs_transfer_status_prefers_success_over_duplicate_timeout(self) -> None:
        signals = [
            {
                "symbol": "ZAMA",
                "checks": {
                    "bn": {
                        "exchange": "bn",
                        "transferStatus": {
                            "exchange": "bn",
                            "status": "error",
                            "message": "handshake timeout",
                        },
                    }
                },
            },
            {
                "symbol": "ZAMA",
                "checks": {
                    "bn": {
                        "exchange": "bn",
                        "transferStatus": {
                            "exchange": "bn",
                            "status": "ok",
                            "message": "Binance 充提状态已更新。",
                            "depositEnabled": True,
                            "withdrawEnabled": True,
                        },
                    }
                },
            },
        ]

        harmonize_fs_transfer_statuses(signals)

        self.assertEqual(signals[0]["checks"]["bn"]["transferStatus"]["status"], "ok")
        self.assertEqual(signals[1]["checks"]["bn"]["transferStatus"]["status"], "ok")

    def test_small_borrow_inventory_still_has_a_period_cost(self) -> None:
        check = MarginShortCheck(
            exchange="bg",
            symbol="BARD",
            status="not_borrowable",
            message="额度小于执行门槛",
            can_borrow=False,
            inventory_available=True,
            borrowable_amount=163.74,
            hourly_borrow_rate=0.00001162,
        )

        self.assertAlmostEqual(borrow_period_rate(check, 4), 0.00004648)

    def test_no_inventory_still_keeps_reference_borrow_cost(self) -> None:
        check = MarginShortCheck(
            exchange="bg",
            symbol="RIF",
            status="not_borrowable",
            message="当前新增可借为 0",
            can_borrow=False,
            inventory_available=False,
            hourly_borrow_rate=0.0003337,
            daily_borrow_rate=0.0080088,
        )
        self.assertAlmostEqual(borrow_period_rate(check, 1), 0.0003337)

    def test_fs_normalization_backfills_small_inventory_cost(self) -> None:
        payload = normalize_fs_signals_payload(
            {
                "items": [
                    {
                        "signalKey": "BARD",
                        "symbol": "BARD",
                        "spotExchange": "bg",
                        "periodHours": 4,
                        "currentFundingRate": -0.001,
                        "premiumRate": -0.001,
                        "feeRate": 0.0008,
                        "slippageRate": 0.0005,
                        "basisRiskRate": 0.0,
                        "checks": {
                            "bg": {
                                "exchange": "bg",
                                "status": "not_borrowable",
                                "message": "额度小于执行门槛",
                                "canBorrow": False,
                                "inventoryAvailable": True,
                                "borrowableAmount": 163.74,
                                "hourlyBorrowRate": 0.00001162,
                                "borrowPeriodRate": None,
                            }
                        },
                        "actionable": False,
                        "watchOnly": True,
                        "pushed": False,
                    }
                ]
            }
        )

        item = payload["items"][0]
        self.assertAlmostEqual(item["checks"]["bg"]["borrowPeriodRate"], 0.00004648)
        self.assertAlmostEqual(item["borrowPeriodRate"], 0.00004648)

    def test_stock_quote_number_parses_market_display_values(self) -> None:
        self.assertEqual(stock_quote_number("$152.35"), 152.35)
        self.assertEqual(stock_quote_number("1,806,000"), 1_806_000)

    @patch("app.crypto.external_stock_json")
    def test_usd_cny_quote_reads_direct_yahoo_rate(self, external_stock_json) -> None:
        external_stock_json.return_value = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": 6.8,
                            "regularMarketTime": 1_700_000_000,
                        }
                    }
                ]
            }
        }
        rate, reference_time, source_name, _source_url = usd_cny_quote(object())
        self.assertEqual(rate, 6.8)
        self.assertEqual(reference_time, datetime.fromtimestamp(1_700_000_000, tz=timezone.utc))
        self.assertEqual(source_name, "Yahoo Finance")

    @patch("app.crypto.external_stock_json")
    @patch("app.crypto.http_client")
    def test_sk_hynix_card_uses_real_us_and_korean_stock_prices(
        self,
        http_client,
        external_stock_json,
    ) -> None:
        http_client.return_value.__enter__.return_value = object()

        def payload_for_url(_client, url):
            if "extended-trading" in url and "markettype=post" in url:
                return {
                    "data": {
                        "lastUpdateInfo": ["Data last updated Jul 13, 2026 08:00 PM ET."],
                        "infoTable": {"rows": [{"consolidated": "$152.6 -15.41 (-9.17%)"}]},
                        "tradeDetailTable": {"rows": [{"time": "19:59:59", "price": "$152.6"}]},
                    }
                }
            if "extended-trading" in url:
                return {"data": {"infoTable": {"rows": None}, "tradeDetailTable": {"rows": None}}}
            if "nasdaq.com" in url:
                return {
                    "data": {
                        "marketStatus": "After-Hours",
                        "primaryData": {"lastSalePrice": "$152.58", "lastTradeTimestamp": "Jul 13, 2026 7:59 PM ET"},
                        "secondaryData": {"lastSalePrice": "$152.35", "lastTradeTimestamp": "Closed at Jul 13, 2026 4:00 PM ET"},
                    }
                }
            if "/api/stock/000660/basic" in url:
                return {
                    "closePrice": "1,806,000",
                    "marketStatus": "OPEN",
                    "tradeStopType": {"name": "TRADING"},
                    "localTradedAt": "2026-07-14T09:45:21+09:00",
                }
            return {
                "result": {
                    "closePrice": "1,498.70",
                    "localTradedAt": "2026-07-14T09:44:33+09:00",
                }
            }

        external_stock_json.side_effect = payload_for_url
        payload = crypto_sk_hynix_us_kr_spread(datetime(2026, 7, 14, 0, 30, tzinfo=timezone.utc), cache_seconds=0)
        expected_korean_usd = 1_806_000 / 10 / 1_498.70
        self.assertEqual(payload["activeMarket"], "kr")
        self.assertFalse(payload["usSession"]["isOpen"])
        self.assertTrue(payload["krSession"]["isOpen"])
        self.assertEqual(payload["latest"]["leftPrice"], 152.60)
        self.assertEqual(payload["usSession"]["priceMode"], "after_hours")
        self.assertAlmostEqual(payload["latest"]["rightPrice"], expected_korean_usd)
        self.assertEqual(payload["krSession"]["rawPrice"], 1_806_000)
        self.assertEqual(payload["fxRate"], 1_498.70)
        self.assertAlmostEqual(payload["standardPremiumPct"], (152.60 / expected_korean_usd - 1) * 100)

    @patch("app.crypto.naver_usd_krw_daily_close_rows")
    @patch("app.crypto.naver_stock_daily_close_rows")
    @patch("app.crypto.nasdaq_stock_daily_close_rows")
    @patch("app.crypto.http_client")
    def test_sk_hynix_history_uses_real_stock_closes_and_daily_fx(
        self,
        http_client,
        nasdaq_rows,
        naver_stock_rows,
        naver_fx_rows,
    ) -> None:
        http_client.return_value.__enter__.return_value = object()
        nasdaq_rows.return_value = {
            "2026-07-21": 150.0,
            "2026-07-22": 165.0,
        }
        naver_stock_rows.return_value = {
            "2026-07-21": 1_800_000.0,
            "2026-07-22": 1_830_000.0,
        }
        naver_fx_rows.return_value = {
            "2026-07-21": 1_500.0,
            "2026-07-22": 1_500.0,
        }

        payload = sk_hynix_stock_history(
            datetime(2026, 7, 23, tzinfo=timezone.utc),
            cache_seconds=0,
        )

        self.assertEqual(payload["sampleCount"], 2)
        self.assertEqual(payload["historyStart"], "2026-07-21")
        self.assertEqual(payload["historyEnd"], "2026-07-22")
        self.assertEqual(payload["items"][0]["source"], "daily_close")
        self.assertAlmostEqual(payload["items"][0]["rightPrice"], 120.0)
        self.assertAlmostEqual(payload["items"][0]["standardPremiumPct"], 25.0)


if __name__ == "__main__":
    unittest.main()
