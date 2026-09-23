from __future__ import annotations

import json
import unittest
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.crypto import MarketQuote
from app.models import (
    CryptoMarketQuoteSnapshot,
    CryptoSymbolMapping,
    ExchangeAnnouncementChangeLog,
    ExchangeAnnouncementPushLog,
    ExchangeAnnouncementTimeline,
    ExchangeDelistingOpportunityAlertLog,
    ExchangeDelistingOpportunityMute,
    ExchangeDelistingOpportunityPairExclusion,
    ExchangeDelistingOpportunitySnapshot,
    ExchangeDelistingOpportunityWatch,
)
from app.exchange_announcements import (
    BEIJING_TZ,
    MARKET_STATUS_EXCHANGES,
    build_item,
    build_opportunity_snapshot,
    build_exchange_announcement_push_body,
    bybit_stock_symbols_from_instruments,
    asset_type_from_text,
    delete_opportunity_pair_monitor,
    delisting_opportunities_overview,
    extract_aster_announcement_rows,
    extract_aster_pending_market_rows,
    extract_event_at,
    extract_gate_next_data_articles,
    extract_okx_app_state_articles,
    extract_symbols_from_text,
    filter_active_announcements,
    fetch_announcement_sources,
    maybe_push_opportunity_alert,
    market_type_from_title,
    market_status_cache_ttl,
    market_status_failure_state,
    market_inventory_delta_items_from_symbols,
    load_opportunity_routes,
    opportunity_alert_policy,
    priority_listing_contract_refresh_keys,
    propagate_stock_asset_classification,
    remaining_routes_for_symbol,
    record_announcement_change,
    reconcile_market_inventory_changes,
    scan_delisting_opportunities,
    set_opportunity_symbol_mute,
    push_exchange_announcements,
    redact_monitor_text,
    supported_opportunity_route_pairs,
    supported_opportunity_watch_pairs,
    sync_delisting_opportunity_watches,
    sync_announcement_timelines,
    symbol_market_statuses,
)


def market_snapshot(
    *,
    live_spot: dict[str, set[str]] | None = None,
    live_contract: dict[str, set[str]] | None = None,
) -> dict[tuple[str, str], dict[str, object]]:
    live_spot = live_spot or {}
    live_contract = live_contract or {}
    snapshot: dict[tuple[str, str], dict[str, object]] = {}
    for exchange, _crypto_exchange, _exchange_name in MARKET_STATUS_EXCHANGES:
        snapshot[(exchange, "spot")] = {
            "status": "ok",
            "symbols": live_spot.get(exchange, set()),
            "message": None,
        }
        snapshot[(exchange, "contract")] = {
            "status": "ok",
            "symbols": live_contract.get(exchange, set()),
            "message": None,
        }
    return snapshot


def announcement_item(event_at: datetime) -> dict[str, object]:
    return {
        "symbol": "AERGO",
        "title": "AERGO USDT合约 下架：Binance",
        "announcements": [
            {
                "exchange": "bn",
                "exchange_name": "Binance",
                "action": "delisting",
                "market_type": "contract",
                "market_label": "USDT合约",
                "event_at": event_at.isoformat(),
            }
        ],
    }


class ExchangeAnnouncementPushBodyTest(unittest.TestCase):
    def test_okx_ssr_payload_recovers_current_articles_and_real_publish_time(self) -> None:
        payload = {
            "appContext": {
                "initialProps": {
                    "sectionData": {
                        "articleList": {
                            "list": [
                                {
                                    "id": "okx-to-list-spkusd-x-perp",
                                    "slug": "okx-to-list-spkusd-x-perp",
                                    "title": "OKX to list SPKUSD X-Perp",
                                    "publishTime": 1787886000000,
                                    "sectionSlug": "announcements-new-listings",
                                }
                            ]
                        }
                    }
                }
            }
        }
        html = f'<script id="appState" type="application/json">{json.dumps(payload)}</script>'

        rows = extract_okx_app_state_articles(
            html,
            source_url="https://www.okx.com/en-ar/help/section/announcements-new-listings",
            default_action="listing",
            default_market="spot",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbols"], ["SPK"])
        self.assertEqual(rows[0]["market_type"], "contract_usd")
        self.assertEqual(rows[0]["published_at"], "2026-08-28T03:00:00+00:00")

    def test_extended_event_actions_and_tradfi_classification(self) -> None:
        self.assertEqual(
            build_item(
                exchange="okx",
                action="unknown",
                market_type="contract",
                title="OKX to suspend ABCUSDT perpetual trading",
                url="https://example.test/suspend",
                published_at="2026-08-31T10:00:00+08:00",
                category="Trading updates",
            )["action"],
            "suspension",
        )
        self.assertEqual(
            build_item(
                exchange="okx",
                action="unknown",
                market_type="contract",
                title="OKX to resume ABCUSDT perpetual trading",
                url="https://example.test/resume",
                published_at="2026-08-31T10:00:00+08:00",
                category="Trading updates",
            )["action"],
            "resumption",
        )
        self.assertEqual(asset_type_from_text("EWZUSDT TradFi perpetual", ["EWZ"]), "stock")

    def test_stock_classification_propagates_to_rename_notice(self) -> None:
        items = [
            {"asset_type": "stock", "asset_label": "股票", "symbols": ["JD", "AMZN"]},
            {"asset_type": "crypto", "asset_label": "加密货币", "symbols": ["JD", "JDON"]},
        ]

        propagate_stock_asset_classification(items)

        self.assertEqual(items[1]["asset_type"], "stock")

    def test_market_inventory_delta_bootstraps_then_detects_addition(self) -> None:
        now = datetime(2026, 8, 31, 20, 0, tzinfo=BEIJING_TZ)
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "discovery.json"
            first = market_inventory_delta_items_from_symbols(
                announcement_exchange="hl",
                market_type="contract",
                current_symbols={"BTC", "ETH"},
                official_items=[],
                state_path=path,
                now=now,
            )
            second = market_inventory_delta_items_from_symbols(
                announcement_exchange="hl",
                market_type="contract",
                current_symbols={"BTC", "ETH", "NEWCOIN"},
                official_items=[],
                state_path=path,
                now=now + timedelta(minutes=1),
            )

        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["symbols"], ["NEWCOIN"])
        self.assertEqual(second[0]["discovery_source"], "market_inventory_delta")
        self.assertIsNone(second[0]["published_at"])

    def test_inventory_reconciliation_records_unmatched_change_once(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeAnnouncementChangeLog.__table__.create(engine)
        now = datetime(2026, 8, 31, 20, 0, tzinfo=BEIJING_TZ)
        with (
            tempfile.TemporaryDirectory() as temporary_dir,
            Session(engine) as db,
            patch("app.exchange_announcements.exchange_monitor_log"),
        ):
            path = Path(temporary_dir) / "reconcile.json"
            baseline = {("okx", "contract"): {"status": "ok", "symbols": {"BTC"}}}
            changed = {("okx", "contract"): {"status": "ok", "symbols": {"BTC", "SPK"}}}
            self.assertEqual(
                reconcile_market_inventory_changes(
                    db,
                    items=[],
                    snapshot=baseline,
                    run_id="baseline",
                    state_path=path,
                    now=now,
                ),
                0,
            )
            self.assertEqual(
                reconcile_market_inventory_changes(
                    db,
                    items=[],
                    snapshot=changed,
                    run_id="changed",
                    state_path=path,
                    now=now + timedelta(minutes=5),
                ),
                1,
            )
            self.assertEqual(
                reconcile_market_inventory_changes(
                    db,
                    items=[],
                    snapshot=changed,
                    run_id="again",
                    state_path=path,
                    now=now + timedelta(minutes=10),
                ),
                0,
            )
            db.commit()
            changes = list(db.scalars(select(ExchangeAnnouncementChangeLog)))

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].change_type, "inventory_without_notice")

    def test_inventory_reconciliation_rebaselines_legacy_state(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeAnnouncementChangeLog.__table__.create(engine)
        now = datetime(2026, 8, 31, 20, 0, tzinfo=BEIJING_TZ)
        with (
            tempfile.TemporaryDirectory() as temporary_dir,
            Session(engine) as db,
            patch("app.exchange_announcements.exchange_monitor_log"),
        ):
            path = Path(temporary_dir) / "reconcile.json"
            path.write_text(
                json.dumps({"inventories": {"gate:spot": []}}),
                encoding="utf-8",
            )
            snapshot = {
                ("gate", "spot"): {
                    "status": "ok",
                    "symbols": {"BTC", "ETH", "XYO"},
                }
            }

            self.assertEqual(
                reconcile_market_inventory_changes(
                    db,
                    items=[],
                    snapshot=snapshot,
                    run_id="legacy-state",
                    state_path=path,
                    now=now,
                ),
                0,
            )
            db.commit()
            changes = list(db.scalars(select(ExchangeAnnouncementChangeLog)))
            stored = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(changes, [])
        self.assertEqual(stored["version"], 1)
        self.assertEqual(stored["inventories"]["gate:spot"], ["BTC", "ETH", "XYO"])

    def test_market_status_failure_keeps_last_success_and_retries_quickly(self) -> None:
        previous = {
            ("bg", "contract"): {
                "status": "ok",
                "symbols": {"PONS"},
                "message": None,
            }
        }

        state = market_status_failure_state(
            ("bg", "contract"),
            "timeout",
            previous,
        )

        self.assertEqual(state["status"], "stale")
        self.assertEqual(state["symbols"], {"PONS"})
        self.assertEqual(market_status_cache_ttl({("bg", "contract"): state}), 15)
        statuses = symbol_market_statuses("PONS", {("bg", "contract"): state})
        bitget = next(row for row in statuses if row["exchange"] == "bg")
        self.assertTrue(bitget["contract"])

    def test_launch_window_uses_contract_only_fast_refresh(self) -> None:
        item = {
            "symbol": "PONS",
            "event_at": datetime.now(BEIJING_TZ).isoformat(),
            "announcements": [
                {
                    "exchange": "bg",
                    "action": "listing",
                    "market_type": "contract",
                }
            ],
        }

        keys = priority_listing_contract_refresh_keys([item])

        self.assertIn(("bg", "contract"), keys)
        self.assertIn(("aster", "contract"), keys)
        self.assertNotIn(("bg", "spot"), keys)

    def test_source_refresh_reuses_fresh_per_source_cache(self) -> None:
        from app import exchange_announcements as module

        module._source_cache.clear()
        try:
            with patch(
                "app.exchange_announcements.fetch_sources",
                return_value=[("bn", [{"title": "new"}], None)],
            ) as fetch_sources:
                first = fetch_announcement_sources([("bn", lambda: [])], force_refresh=True)
                second = fetch_announcement_sources([("bn", lambda: [])], force_refresh=False)

            self.assertEqual(first[0][1], [{"title": "new"}])
            self.assertEqual(first[0][2]["status"], "ok")
            self.assertFalse(first[0][2]["cache_hit"])
            self.assertTrue(second[0][2]["cache_hit"])
            self.assertEqual(fetch_sources.call_count, 1)
        finally:
            module._source_cache.clear()

    def test_listing_timeline_records_market_open_and_astro_registration(self) -> None:
        now = datetime.now(BEIJING_TZ)
        item = {
            "symbol": "NEWCOIN",
            "title": "NEWCOIN USDT合约 上架：Binance",
            "latest_published_at": (now - timedelta(minutes=5)).isoformat(),
            "event_at": now.isoformat(),
            "announcements": [
                {
                    "exchange": "bn",
                    "exchange_name": "Binance",
                    "action": "listing",
                    "market_type": "contract",
                    "published_at": (now - timedelta(minutes=5)).isoformat(),
                    "event_at": now.isoformat(),
                    "url": "https://example.com/newcoin",
                }
            ],
        }
        # Even when the broad market-status sweep times out, an Astro direct
        # route registration is enough evidence that the listing market opened.
        snapshot = market_snapshot()
        engine = create_engine("sqlite:///:memory:")
        ExchangeAnnouncementTimeline.__table__.create(engine)
        with (
            Session(engine) as db,
            patch(
                "app.astro_spread_scanner.astro_listing_linkage_status",
                return_value={
                    "NEWCOIN": {
                        "status": "registered",
                        "reason": "watching",
                        "registeredAt": now.isoformat(),
                        "firstDirectCheckAt": None,
                    }
                },
            ),
        ):
            sync_announcement_timelines(db, [item], snapshot)
            db.commit()
            timeline = db.scalar(select(ExchangeAnnouncementTimeline))

        self.assertIsNotNone(timeline)
        assert timeline is not None
        self.assertIsNotNone(timeline.market_opened_at)
        self.assertEqual(timeline.astro_status, "registered")
        self.assertIsNotNone(timeline.astro_registered_at)

    def test_listing_timeline_second_sync_does_not_write_unchanged_state(self) -> None:
        now = datetime.now(BEIJING_TZ)
        item = {
            "symbol": "NEWCOIN",
            "title": "NEWCOIN USDT合约 上架：Binance",
            "latest_published_at": (now - timedelta(minutes=5)).isoformat(),
            "event_at": now.isoformat(),
            "announcements": [
                {
                    "exchange": "bn",
                    "action": "listing",
                    "market_type": "contract",
                    "published_at": (now - timedelta(minutes=5)).isoformat(),
                    "event_at": now.isoformat(),
                }
            ],
        }
        engine = create_engine("sqlite:///:memory:")
        ExchangeAnnouncementTimeline.__table__.create(engine)
        linkage = {
            "NEWCOIN": {
                "status": "registered",
                "reason": "watching",
                "registeredAt": now.isoformat(),
            }
        }
        with Session(engine) as db, patch(
            "app.astro_spread_scanner.astro_listing_linkage_status",
            return_value=linkage,
        ):
            self.assertEqual(sync_announcement_timelines(db, [item], market_snapshot()), 1)
            db.commit()
            timeline = db.scalar(select(ExchangeAnnouncementTimeline))
            assert timeline is not None
            first_updated_at = timeline.updated_at
            self.assertEqual(sync_announcement_timelines(db, [item], market_snapshot()), 0)
            db.commit()
            self.assertEqual(timeline.updated_at, first_updated_at)

    def test_announcement_change_log_is_idempotent(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeAnnouncementChangeLog.__table__.create(engine)
        with Session(engine) as db:
            first = record_announcement_change(
                db,
                announcement_key="key",
                symbol="COIN",
                change_type="event_time_changed",
                summary="COIN 改期",
            )
            db.flush()
            second = record_announcement_change(
                db,
                announcement_key="key",
                symbol="COIN",
                change_type="event_time_changed",
                summary="COIN 改期",
            )

        self.assertTrue(first)
        self.assertFalse(second)

    def test_chinese_contract_symbol_is_extracted_from_binance_and_gate_titles(self) -> None:
        self.assertEqual(
            extract_symbols_from_text(
                "币安合约将上线 牛来USDT U本位永续合约 (2026-08-30)"
            ),
            ["牛来"],
        )
        self.assertEqual(
            extract_symbols_from_text("Gate launches 牛来 (USDT-M) perpetual contract"),
            ["牛来"],
        )

    def test_gate_next_data_uses_structured_rows_and_stock_markers(self) -> None:
        html = """
        <html><body>
          <script id="__NEXT_DATA__" type="application/json">
          {
            "props": {
              "pageProps": {
                "listData": {
                  "list": [
                    {
                      "id": 100902,
                      "title": "Gate gStocks 专区新增 ZHIPUG（智谱 AI）",
                      "brief": "于 2026 年 7 月 30 日 14:00 (UTC+8) 上线代币化证券",
                      "release_timestamp": "1785324486",
                      "tags": "ZHIPUG、MINIMAXG",
                      "cate_id": 38,
                      "url": "/announcements/article/100902"
                    },
                    {
                      "id": 100857,
                      "title": "Gate 将上线 AEON (AEON) 合约交易",
                      "brief": "AEONUSDT 永续合约于 2026 年 7 月 28 日 16:00 (UTC+8) 上线",
                      "release_timestamp": "1785207293",
                      "tags": "AEON",
                      "cate_id": 37,
                      "url": "/announcements/article/100857"
                    }
                  ]
                }
              }
            }
          }
          </script>
        </body></html>
        """

        rows = extract_gate_next_data_articles(
            html,
            source_url="https://www.gate.tv/zh/announcements/newlisted",
            default_action="listing",
            default_market="unknown",
            category="Gate 公告",
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["market_type"], "spot")
        self.assertEqual(rows[0]["asset_type"], "stock")
        self.assertEqual(rows[0]["symbols"], ["ZHIPUG", "MINIMAXG"])
        self.assertEqual(
            rows[0]["url"],
            "https://www.gate.tv/announcements/article/100902",
        )
        self.assertEqual(rows[1]["market_type"], "contract")
        self.assertEqual(rows[1]["asset_type"], "crypto")
        self.assertEqual(rows[1]["symbols"], ["AEON"])

    def test_stock_keyword_matching_ignores_random_url_id_substrings(self) -> None:
        self.assertEqual(
            asset_type_from_text(
                "New listing: REZUSDT https://example.com/article/artcfd123",
                ["REZ"],
            ),
            "crypto",
        )
        self.assertEqual(
            asset_type_from_text(
                "New listing: GMESTOCKUSDT perpetual contract",
                ["GMESTOCK"],
            ),
            "stock",
        )

    def test_bybit_instrument_metadata_identifies_stock_contracts(self) -> None:
        symbols = bybit_stock_symbols_from_instruments(
            {
                "result": {
                    "list": [
                        {"symbol": "TENCENTUSDT", "symbolType": "stock"},
                        {"symbol": "POPMARTUSDT", "symbolType": "stock"},
                        {"symbol": "REZUSDT", "symbolType": "innovation"},
                    ]
                }
            }
        )

        self.assertEqual(symbols, {"TENCENT", "POPMART"})

    def test_perpetual_contract_title_wins_over_leverage_wording(self) -> None:
        self.assertEqual(
            market_type_from_title(
                "New listing: TENCENTUSDT Perpetual Contract, with up to 25x leverage"
            ),
            "contract",
        )

    def test_equity_listing_slug_marks_stock_when_headline_omits_asset_type(self) -> None:
        item = build_item(
            exchange="okx",
            action="listing",
            market_type="contract",
            title="欧易关于 TMF 永续合约正式上线的公告",
            url="https://www.okx.com/zh-hans/help/okx-to-list-perpetual-futures-for-tmf-equity",
            published_at="2026-07-29T00:00:00+00:00",
            category="OKX New Listings",
        )

        self.assertEqual(item["symbols"], ["TMF"])
        self.assertEqual(item["asset_type"], "stock")
        self.assertEqual(item["asset_label"], "股票")

    def test_bybit_description_time_with_spaces_uses_utc(self) -> None:
        event_at = extract_event_at(
            "Bybit 於 2026 年 7 月 25 日9:00 UTC 下架 AERGOUSDT 永續合約。"
        )

        self.assertEqual(event_at, "2026-07-25T17:00:00+08:00")

    def test_monitor_log_redacts_bark_url_and_tokens(self) -> None:
        with patch.dict(
            os.environ,
            {"BARK_WEBHOOK_URL": "https://bark.example/secret-device"},
            clear=False,
        ):
            redacted = redact_monitor_text(
                "request failed https://bark.example/secret-device/path?token=abc123"
            )

        self.assertNotIn("secret-device", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertIn("<redacted-bark-url>", redacted)
        self.assertIn("token=<redacted>", redacted)

    def test_aster_delisting_rows_include_contract_time_and_official_link(self) -> None:
        rows = extract_aster_announcement_rows(
            {
                "data": {
                    "rows": [
                        {
                            "id": 381,
                            "title": (
                                "[Delisting Notice] 1000SATSUSDT Perpetual "
                                "- 2026-07-24 09:00 UTC"
                            ),
                            "publishTime": 1784779630000,
                        }
                    ]
                }
            },
            action="delisting",
            category_code="DELISTING",
            category_label="Aster Delistings",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exchange"], "aster")
        self.assertEqual(rows[0]["symbols"], ["1000SATS"])
        self.assertEqual(rows[0]["market_type"], "contract")
        self.assertEqual(rows[0]["event_at"], "2026-07-24T17:00:00+08:00")
        self.assertEqual(
            rows[0]["url"],
            "https://www.asterdex.com/en/announcement/381?category=DELISTING",
        )

    def test_aster_listing_accepts_digit_prefixed_symbol(self) -> None:
        rows = extract_aster_announcement_rows(
            {
                "data": {
                    "rows": [
                        {
                            "id": 353,
                            "title": "New Perp Listing: $1000XEC(5x)",
                            "publishTime": 1784381973000,
                        }
                    ]
                }
            },
            action="listing",
            category_code="NEW_LISTING",
            category_label="Aster New Listings",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbols"], ["1000XEC"])

    def test_aster_pending_market_row_uses_onboard_time(self) -> None:
        rows = extract_aster_pending_market_rows(
            {
                "symbols": [
                    {
                        "symbol": "CXMTUSDT",
                        "status": "PENDING_TRADING",
                        "baseAsset": "CXMT",
                        "quoteAsset": "USDT",
                        "onboardDate": 1785103200000,
                    }
                ]
            },
            observed_at=datetime(2026, 7, 26, 10, 0, tzinfo=BEIJING_TZ),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbols"], ["CXMT"])
        self.assertEqual(rows[0]["market_type"], "contract")
        self.assertEqual(rows[0]["asset_type"], "stock")
        self.assertEqual(rows[0]["asset_label"], "股票")
        self.assertEqual(rows[0]["event_at"], "2026-07-27T06:00:00+08:00")
        self.assertEqual(rows[0]["category"], "Aster Market Pre-listings")
        self.assertEqual(
            rows[0]["url"],
            (
                "https://www.asterdex.com/en/announcement"
                "?category=NEW_LISTING&symbol=CXMTUSDT"
            ),
        )

    def test_upcoming_listing_is_kept_before_listing_day(self) -> None:
        now = datetime.now(BEIJING_TZ)
        event_at = now + timedelta(hours=12)
        item = {
            "action": "listing",
            "event_at": event_at.isoformat(),
            "published_at": now.isoformat(),
        }

        active, expired = filter_active_announcements([item])

        self.assertEqual(active, [item])
        self.assertEqual(expired, 0)

    def test_aster_listing_remains_when_status_turns_trading_early(self) -> None:
        rows = extract_aster_pending_market_rows(
            {
                "symbols": [
                    {
                        "symbol": "CXMTUSDT",
                        "status": "TRADING",
                        "baseAsset": "CXMT",
                        "quoteAsset": "USDT",
                        "onboardDate": 1785103200000,
                    }
                ]
            },
            observed_at=datetime(2026, 7, 26, 20, 30, tzinfo=BEIJING_TZ),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbols"], ["CXMT"])
        self.assertEqual(rows[0]["event_at"], "2026-07-27T06:00:00+08:00")
        self.assertEqual(rows[0]["asset_type"], "stock")

    def test_body_includes_delisting_time_and_remaining_live_markets(self) -> None:
        event_at = datetime.now(BEIJING_TZ) - timedelta(hours=1)
        body = build_exchange_announcement_push_body(
            announcement_item(event_at),
            market_snapshot(
                live_spot={"okx": {"AERGO"}},
                live_contract={"by": {"AERGO"}},
            ),
        )

        self.assertIn(
            f"变动：Binance合约已于{event_at.strftime('%Y-%m-%d %H:%M')}下架",
            body,
        )
        self.assertIn("当前可交易（USDT）：Bybit 合约｜OKX 现货", body)
        self.assertIn("未查到USDT市场：Binance/Bitget/Gate", body)
        self.assertNotIn("核验差异", body)

    def test_body_reports_announcement_and_live_market_conflict(self) -> None:
        event_at = datetime.now(BEIJING_TZ) - timedelta(minutes=5)
        body = build_exchange_announcement_push_body(
            announcement_item(event_at),
            market_snapshot(live_contract={"bn": {"AERGO"}}),
        )

        self.assertIn("当前可交易（USDT）：Binance 合约", body)
        self.assertIn("公告已到下架时间，但实时接口仍显示可交易", body)

    def test_body_marks_failed_market_checks_as_unknown(self) -> None:
        event_at = datetime.now(BEIJING_TZ) + timedelta(hours=1)
        snapshot = market_snapshot(live_contract={"bn": {"AERGO"}})
        snapshot[("gate", "spot")] = {
            "status": "error",
            "symbols": set(),
            "message": "timeout",
        }

        body = build_exchange_announcement_push_body(announcement_item(event_at), snapshot)

        self.assertIn("状态核验失败：Gate现货", body)
        self.assertNotIn("未查到USDT市场：Binance/Bitget/Gate", body)

    def test_new_announcement_push_uses_enriched_body(self) -> None:
        event_at = datetime.now(BEIJING_TZ) - timedelta(hours=1)
        item = {
            **announcement_item(event_at),
            "event_at": event_at.isoformat(),
            "asset_type": "crypto",
            "asset_label": "加密货币",
        }
        snapshot = market_snapshot(
            live_spot={"okx": {"AERGO"}},
            live_contract={"by": {"AERGO"}},
        )
        engine = create_engine("sqlite:///:memory:")
        ExchangeAnnouncementPushLog.__table__.create(engine)
        ExchangeAnnouncementTimeline.__table__.create(engine)
        ExchangeAnnouncementChangeLog.__table__.create(engine)
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        with (
            tempfile.TemporaryDirectory() as state_dir,
            Session(engine) as db,
            patch(
                "app.exchange_announcements.get_exchange_announcements",
                return_value={"status": "ok", "items": [item]},
            ),
            patch(
                "app.exchange_announcements.exchange_market_status_snapshot",
                return_value=snapshot,
            ),
            patch(
                "app.exchange_announcements.send_bark_or_log",
                return_value=("ok", None),
            ) as send_bark,
            patch("app.exchange_announcements.exchange_monitor_log"),
            patch("app.exchange_announcements.bark_status", return_value="ok"),
            patch("app.exchange_announcements.exchange_push_status", return_value="ok"),
            patch(
                "app.exchange_announcements.market_inventory_reconciliation_state_path",
                return_value=Path(state_dir) / "reconciliation.json",
            ),
        ):
            result = push_exchange_announcements(db)
            stored = db.scalar(select(ExchangeAnnouncementPushLog))

        self.assertEqual(result["pushed_count"], 1)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertIn("当前可交易（USDT）：Bybit 合约｜OKX 现货", stored.body)
        self.assertIn("当前可交易（USDT）：Bybit 合约｜OKX 现货", send_bark.call_args.kwargs["body"])

    def test_funding_interval_announcement_is_recorded_without_push(self) -> None:
        event_at = datetime.now(BEIJING_TZ) - timedelta(minutes=5)
        item = {
            "symbol": "COTI",
            "title": "COTI USDT合约 合约参数变更：Bitget",
            "event_at": event_at.isoformat(),
            "asset_type": "crypto",
            "asset_label": "加密货币",
            "announcements": [
                {
                    "exchange": "bg",
                    "exchange_name": "Bitget",
                    "action": "parameter_change",
                    "market_type": "contract",
                    "market_label": "USDT合约",
                    "title": "关于 COTIUSDT永续合约资金费率时间周期调整的公告",
                    "url": "https://www.bitget.com/zh-CN/support/articles/interval-test",
                    "event_at": event_at.isoformat(),
                    "symbols": ["COTI"],
                }
            ],
        }
        engine = create_engine("sqlite:///:memory:")
        ExchangeAnnouncementPushLog.__table__.create(engine)
        ExchangeAnnouncementChangeLog.__table__.create(engine)
        with (
            tempfile.TemporaryDirectory() as state_dir,
            Session(engine) as db,
            patch(
                "app.exchange_announcements.get_exchange_announcements",
                return_value={"status": "ok", "items": [item]},
            ),
            patch(
                "app.exchange_announcements.exchange_market_status_snapshot",
                return_value=market_snapshot(),
            ),
            patch("app.exchange_announcements.send_bark_or_log") as send_bark,
            patch("app.exchange_announcements.exchange_monitor_log"),
            patch("app.exchange_announcements.exchange_monitor_log_on_change"),
            patch("app.exchange_announcements.bark_status", return_value="ok"),
            patch("app.exchange_announcements.exchange_push_status", return_value="ok"),
            patch(
                "app.exchange_announcements.market_inventory_reconciliation_state_path",
                return_value=Path(state_dir) / "reconciliation.json",
            ),
        ):
            result = push_exchange_announcements(db)
            stored = db.scalar(select(ExchangeAnnouncementPushLog))

        send_bark.assert_not_called()
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, "suppressed")
        self.assertIn("资金费周期变化按规则仅记录", stored.message)
        self.assertEqual(result["pushed_count"], 0)
        self.assertEqual(result["suppressed_count"], 1)

    def test_delisting_opportunity_uses_remaining_other_markets(self) -> None:
        statuses = [
            {"exchange": "bn", "spot": False, "contract": False},
            {"exchange": "bg", "spot": False, "contract": False},
            {"exchange": "by", "spot": False, "contract": True},
            {"exchange": "gate", "spot": False, "contract": False},
            {"exchange": "okx", "spot": True, "contract": False},
        ]
        routes = remaining_routes_for_symbol(
            "AERGO",
            statuses,
            [{"exchange": "bn", "market_type": "contract"}],
        )

        self.assertEqual(
            routes,
            [
                {"exchange": "by", "market_type": "futures", "symbol": "AERGO"},
            ],
        )

    def test_aster_contract_is_available_as_remaining_opportunity_route(self) -> None:
        statuses = [
            {"exchange": "bn", "spot": False, "contract": False},
            {"exchange": "okx", "spot": True, "contract": False},
            {"exchange": "aster", "spot": False, "contract": True},
        ]

        routes = remaining_routes_for_symbol(
            "AERGO",
            statuses,
            [{"exchange": "bn", "market_type": "contract"}],
        )

        self.assertIn(
            {"exchange": "as", "market_type": "futures", "symbol": "AERGO"},
            routes,
        )

    def test_announced_delisting_route_is_excluded_before_event_time(self) -> None:
        now = datetime.now(BEIJING_TZ)
        statuses = [
            {"exchange": "bn", "spot": False, "contract": True},
            {"exchange": "by", "spot": False, "contract": True},
            {"exchange": "okx", "spot": True, "contract": False},
        ]
        routes = remaining_routes_for_symbol(
            "AERGO",
            statuses,
            [
                {
                    "exchange": "bn",
                    "market_type": "contract",
                    "event_at": (now - timedelta(hours=1)).isoformat(),
                },
                {
                    "exchange": "by",
                    "market_type": "contract",
                    "event_at": (now + timedelta(hours=1)).isoformat(),
                },
            ],
        )

        self.assertNotIn(
            {"exchange": "bn", "market_type": "futures", "symbol": "AERGO"},
            routes,
        )
        self.assertNotIn(
            {"exchange": "by", "market_type": "futures", "symbol": "AERGO"},
            routes,
        )
        self.assertEqual(routes, [])

    def test_spot_delisting_creates_watch_but_excludes_source_exchange(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        event_at = datetime.now(BEIJING_TZ) + timedelta(days=1)
        item = {
            "symbol": "HFT",
            "title": "HFT 现货 下架：Binance",
            "event_at": event_at.isoformat(),
            "announcements": [
                {
                    "exchange": "bn",
                    "action": "delisting",
                    "market_type": "spot",
                    "symbols": ["HFT"],
                }
            ],
        }
        snapshot = market_snapshot(
            live_spot={"bn": {"HFT"}},
            live_contract={"bn": {"HFT"}, "by": {"HFT"}, "gate": {"HFT"}},
        )

        with Session(engine) as db, patch("app.exchange_announcements.exchange_monitor_log"):
            synced = sync_delisting_opportunity_watches(db, [item], snapshot, "hft-spot-test")
            watch = db.scalar(select(ExchangeDelistingOpportunityWatch))

        self.assertEqual(synced, 1)
        self.assertIsNotNone(watch)
        assert watch is not None
        self.assertEqual(watch.source_market_type, "spot")
        routes = json.loads(watch.routes_json)
        self.assertNotIn(
            {"exchange": "bn", "market_type": "futures", "symbol": "HFT", "event_action": "delisting", "event_source": "0"},
            routes,
        )
        self.assertEqual(
            {(route["exchange"], route["market_type"]) for route in routes},
            {("by", "futures"), ("gt", "futures")},
        )
        self.assertEqual(len(supported_opportunity_watch_pairs(routes)), 1)

    def test_only_futures_futures_pairs_are_monitored(self) -> None:
        routes = [
            {"exchange": "bg", "market_type": "spot", "symbol": "ROAM"},
            {"exchange": "by", "market_type": "spot", "symbol": "ROAM"},
            {"exchange": "by", "market_type": "futures", "symbol": "ROAM"},
            {"exchange": "gt", "market_type": "futures", "symbol": "ROAM"},
        ]

        pairs = supported_opportunity_route_pairs(routes)

        self.assertEqual(
            pairs,
            [
                (
                    {"exchange": "by", "market_type": "futures", "symbol": "ROAM"},
                    {"exchange": "gt", "market_type": "futures", "symbol": "ROAM"},
                )
            ],
        )

    def test_listing_opportunity_pairs_must_include_newly_listed_market(self) -> None:
        routes = [
            {
                "exchange": "okx",
                "market_type": "futures",
                "symbol": "AEON",
                "event_action": "listing",
                "event_source": "1",
            },
            {
                "exchange": "bg",
                "market_type": "futures",
                "symbol": "AEON",
                "event_action": "listing",
                "event_source": "0",
            },
            {
                "exchange": "gt",
                "market_type": "spot",
                "symbol": "AEON",
                "event_action": "listing",
                "event_source": "0",
            },
        ]

        pairs = supported_opportunity_watch_pairs(routes)

        self.assertEqual(len(pairs), 1)
        self.assertTrue(
            all(left["event_source"] == "1" or right["event_source"] == "1" for left, right in pairs)
        )

    def test_listing_announcement_creates_live_opportunity_watch(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        event_at = datetime.now(BEIJING_TZ)
        item = {
            "symbol": "AEON",
            "title": "AEON USDT合约 上架：OKX",
            "event_at": event_at.isoformat(),
            "announcements": [
                {
                    "exchange": "okx",
                    "action": "listing",
                    "market_type": "contract",
                    "symbols": ["AEON"],
                }
            ],
        }
        snapshot = market_snapshot(
            live_spot={"gate": {"AEON"}},
            live_contract={"okx": {"AEON"}, "bg": {"AEON"}},
        )
        with Session(engine) as db, patch("app.exchange_announcements.exchange_monitor_log"):
            synced = sync_delisting_opportunity_watches(db, [item], snapshot, "aeon-test")
            db.flush()
            watch = db.scalar(select(ExchangeDelistingOpportunityWatch))

        self.assertEqual(synced, 1)
        self.assertIsNotNone(watch)
        assert watch is not None
        self.assertEqual(watch.status, "watch")
        self.assertTrue(watch.announcement_key.endswith(":listing"))
        routes = json.loads(watch.routes_json)
        pairs = supported_opportunity_watch_pairs(routes)
        self.assertEqual(len(pairs), 1)
        self.assertTrue(
            all(left["event_source"] == "1" or right["event_source"] == "1" for left, right in pairs)
        )

    def test_opportunity_watch_sync_logs_only_when_state_changes(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        event_at = datetime.now(BEIJING_TZ)
        item = {
            "symbol": "AEON",
            "title": "AEON USDT合约 上架：OKX",
            "event_at": event_at.isoformat(),
            "announcements": [
                {
                    "exchange": "okx",
                    "action": "listing",
                    "market_type": "contract",
                    "symbols": ["AEON"],
                }
            ],
        }
        snapshot = market_snapshot(live_contract={"okx": {"AEON"}, "bg": {"AEON"}})
        with Session(engine) as db, patch(
            "app.exchange_announcements.exchange_monitor_log"
        ) as monitor_log:
            self.assertEqual(sync_delisting_opportunity_watches(db, [item], snapshot, "first"), 1)
            db.commit()
            self.assertEqual(sync_delisting_opportunity_watches(db, [item], snapshot, "second"), 0)
            self.assertEqual(monitor_log.call_count, 1)

    def test_listing_bypass_evidence_belongs_only_to_announced_market(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        event_at = datetime.now(BEIJING_TZ) + timedelta(minutes=30)
        item = {
            "symbol": "AEON", "title": "AEON USDT合约 上架：OKX",
            "event_at": event_at.isoformat(),
            "announcements": [{
                "exchange": "okx", "action": "listing", "market_type": "contract",
                "symbols": ["AEON"], "event_at": event_at.isoformat(),
            }],
        }
        snapshot = market_snapshot(
            live_spot={"okx": {"AEON"}}, live_contract={"okx": {"AEON"}, "bg": {"AEON"}},
        )
        with Session(engine) as db, patch("app.exchange_announcements.exchange_monitor_log"):
            sync_delisting_opportunity_watches(db, [item], snapshot, "market-specific-time")
            db.flush()
            watch = db.scalar(select(ExchangeDelistingOpportunityWatch))
            routes = load_opportunity_routes(watch)
        announced = next(r for r in routes if r["exchange"] == "okx" and r["market_type"] == "futures")
        self.assertTrue(announced["listingEventTimeKnown"])
        self.assertEqual(datetime.fromisoformat(announced["listingEventAt"]), event_at)
        self.assertEqual(announced["event_source"], "1")
        others = [r for r in routes if r is not announced]
        self.assertTrue(others)
        for route in others:
            self.assertEqual(route["event_source"], "0")
            self.assertFalse(route["listingEventTimeKnown"])
            self.assertIsNone(route["listingEventAt"])

    def test_listing_unknown_time_does_not_inherit_aggregate_or_now_fallback(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        item = {
            "symbol": "AEON", "title": "AEON USDT合约 上架：OKX",
            "event_at": datetime.now(BEIJING_TZ).isoformat(),
            "announcements": [{
                "exchange": "okx", "action": "listing", "market_type": "contract",
                "symbols": ["AEON"],
            }],
        }
        snapshot = market_snapshot(live_contract={"bg": {"AEON"}})
        with Session(engine) as db, patch("app.exchange_announcements.exchange_monitor_log"):
            sync_delisting_opportunity_watches(db, [item], snapshot, "unknown-time")
            db.flush()
            watch = db.scalar(select(ExchangeDelistingOpportunityWatch))
            routes = load_opportunity_routes(watch)
        source = next(r for r in routes if r["exchange"] == "okx")
        self.assertEqual(source["event_source"], "1")
        self.assertEqual(source["pending"], "1")
        self.assertFalse(source["listingEventTimeKnown"])
        self.assertIsNone(source["listingEventAt"])

    def test_opportunity_market_fetch_runs_without_open_database_transaction(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        for table in (
            CryptoSymbolMapping.__table__,
            ExchangeDelistingOpportunityWatch.__table__,
            ExchangeDelistingOpportunityPairExclusion.__table__,
            CryptoMarketQuoteSnapshot.__table__,
            ExchangeDelistingOpportunitySnapshot.__table__,
        ):
            table.create(engine)
        expires_at = datetime.now(BEIJING_TZ) + timedelta(hours=2)
        fetch_transaction_states: list[bool] = []
        with Session(engine) as db:
            db.add(
                ExchangeDelistingOpportunityWatch(
                    announcement_key="transaction-test",
                    symbol="AERGO",
                    source_exchange="bn",
                    source_market_type="futures",
                    expires_at=expires_at,
                    routes_json=json.dumps(
                        [
                            {"exchange": "bn", "market_type": "futures", "symbol": "AERGO"},
                            {"exchange": "by", "market_type": "futures", "symbol": "AERGO"},
                        ]
                    ),
                )
            )
            db.commit()

            def fake_fetch(exchange: str, symbol: str, market_type: str) -> MarketQuote:
                fetch_transaction_states.append(bool(db.in_transaction()))
                return MarketQuote(
                    exchange=exchange,
                    symbol=symbol,
                    market_type=market_type,
                    best_bid=1.01,
                    best_ask=1.02,
                    volume_24h=1_000_000,
                )

            with (
                patch("app.crypto.fetch_market", side_effect=fake_fetch),
                patch("app.exchange_announcements.exchange_monitor_log"),
                patch("app.exchange_announcements.exchange_monitor_log_on_change"),
            ):
                result = scan_delisting_opportunities(db, push=False)

        self.assertEqual(result["snapshot_count"], 1)
        self.assertEqual(fetch_transaction_states, [False, False])

    def test_listing_announcement_starts_pending_source_watch_before_market_opens(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        item = {
            "symbol": "PONS",
            "title": "PONS USDT合约 上架：Bitget",
            "event_at": datetime.now(BEIJING_TZ).isoformat(),
            "announcements": [
                {
                    "exchange": "bg",
                    "action": "listing",
                    "market_type": "contract",
                    "symbols": ["PONS"],
                }
            ],
        }
        snapshot = market_snapshot(live_contract={"aster": {"PONS"}})

        with Session(engine) as db, patch("app.exchange_announcements.exchange_monitor_log"):
            synced = sync_delisting_opportunity_watches(db, [item], snapshot, "pons-test")
            db.flush()
            watch = db.scalar(select(ExchangeDelistingOpportunityWatch))

        self.assertEqual(synced, 1)
        self.assertIsNotNone(watch)
        assert watch is not None
        self.assertEqual(watch.status, "waiting_source_market")
        routes = json.loads(watch.routes_json)
        bitget = next(route for route in routes if route["exchange"] == "bg")
        self.assertEqual(bitget["event_source"], "1")
        self.assertEqual(bitget["pending"], "1")
        self.assertEqual(len(supported_opportunity_watch_pairs(routes)), 1)

    def test_stock_listing_does_not_create_cross_exchange_opportunity(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        item = {
            "symbol": "KR200",
            "title": "KR200（股票） USDT合约 上架：OKX",
            "asset_type": "stock",
            "event_at": datetime.now(BEIJING_TZ).isoformat(),
            "announcements": [
                {
                    "exchange": "okx",
                    "action": "listing",
                    "market_type": "contract",
                    "symbols": ["KR200"],
                }
            ],
        }
        snapshot = market_snapshot(
            live_contract={"gate": {"KR200"}, "okx": {"KR200"}},
        )
        with Session(engine) as db, patch("app.exchange_announcements.exchange_monitor_log"):
            synced = sync_delisting_opportunity_watches(db, [item], snapshot, "kr200-test")
            db.flush()
            watch = db.scalar(select(ExchangeDelistingOpportunityWatch))

        self.assertEqual(synced, 0)
        self.assertIsNone(watch)

    def test_stock_push_body_only_checks_announced_market(self) -> None:
        item = {
            "symbol": "KR200",
            "title": "KR200（股票） USDT合约 上架：OKX",
            "asset_type": "stock",
            "announcements": [
                {
                    "exchange": "okx",
                    "exchange_name": "OKX",
                    "action": "listing",
                    "market_type": "contract",
                }
            ],
        }
        snapshot = market_snapshot(
            live_contract={"gate": {"KR200"}, "okx": {"KR200"}},
        )

        body = build_exchange_announcement_push_body(item, snapshot)

        self.assertIn("公告对应市场（股票/指数需核验底层与报价单位）：OKX 合约", body)
        self.assertNotIn("Gate 合约", body)

    def test_opportunity_snapshot_uses_astro_executable_books(self) -> None:
        watch = ExchangeDelistingOpportunityWatch(
            id=1,
            announcement_key="aergo-test",
            symbol="AERGO",
            source_exchange="bn",
            source_market_type="futures",
            event_at=datetime.now(BEIJING_TZ),
            expires_at=datetime.now(BEIJING_TZ) + timedelta(hours=72),
            routes_json="[]",
        )
        left_route = {"exchange": "okx", "market_type": "futures", "symbol": "AERGO"}
        right_route = {"exchange": "by", "market_type": "futures", "symbol": "AERGO"}
        left_quote = MarketQuote(
            exchange="okx",
            symbol="AERGO",
            market_type="futures",
            status="ok",
            best_bid=1.02,
            best_ask=1.021,
            volume_24h=1_000_000,
        )
        right_quote = MarketQuote(
            exchange="by",
            symbol="AERGO",
            market_type="futures",
            status="ok",
            best_bid=0.99,
            best_ask=1.0,
            volume_24h=2_000_000,
        )

        snapshot = build_opportunity_snapshot(
            watch,
            left_route,
            right_route,
            left_quote,
            right_quote,
        )

        expected = 2 * (1.02 - 1.0) / (1.02 + 1.0) * 100
        self.assertEqual(snapshot.pair_type, "FF")
        self.assertAlmostEqual(snapshot.sell_right_buy_left_pct or 0, expected)
        self.assertAlmostEqual(snapshot.best_executable_spread_pct or 0, expected)
        self.assertEqual(snapshot.direction, "卖出OKX合约 / 买入Bybit合约")

    def test_opportunity_alert_policy_uses_strict_three_tiers(self) -> None:
        with patch.dict(
            os.environ,
            {
                "EXCHANGE_OPPORTUNITY_ALERT_PCT": "1",
                "EXCHANGE_OPPORTUNITY_COOLDOWN_MINUTES": "30",
                "EXCHANGE_OPPORTUNITY_HIGH_ALERT_PCT": "5",
                "EXCHANGE_OPPORTUNITY_HIGH_COOLDOWN_MINUTES": "5",
                "EXCHANGE_OPPORTUNITY_CRITICAL_ALERT_PCT": "10",
                "EXCHANGE_OPPORTUNITY_CRITICAL_COOLDOWN_MINUTES": "1",
            },
            clear=False,
        ):
            self.assertIsNone(opportunity_alert_policy(1.0))
            self.assertEqual(opportunity_alert_policy(1.01), ("normal", 30))
            self.assertEqual(opportunity_alert_policy(5.0), ("normal", 30))
            self.assertEqual(opportunity_alert_policy(5.01), ("high", 5))
            self.assertEqual(opportunity_alert_policy(10.0), ("high", 5))
            self.assertEqual(opportunity_alert_policy(10.01), ("critical", 1))

    def test_tier_upgrades_alert_immediately_then_apply_own_cooldown(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        ExchangeDelistingOpportunityMute.__table__.create(engine)
        ExchangeDelistingOpportunityAlertLog.__table__.create(engine)
        with (
            Session(engine) as db,
            patch.dict(
                os.environ,
                {
                    "EXCHANGE_OPPORTUNITY_ALERT_PCT": "1",
                    "EXCHANGE_OPPORTUNITY_COOLDOWN_MINUTES": "30",
                    "EXCHANGE_OPPORTUNITY_HIGH_ALERT_PCT": "5",
                    "EXCHANGE_OPPORTUNITY_HIGH_COOLDOWN_MINUTES": "5",
                    "EXCHANGE_OPPORTUNITY_CRITICAL_ALERT_PCT": "10",
                    "EXCHANGE_OPPORTUNITY_CRITICAL_COOLDOWN_MINUTES": "1",
                },
                clear=False,
            ),
            patch(
                "app.exchange_announcements.send_bark_or_log",
                return_value=("ok", None),
            ) as send_bark,
        ):
            watch = ExchangeDelistingOpportunityWatch(
                announcement_key="aergo-alert-test",
                symbol="AERGO",
                source_exchange="bn",
                source_market_type="futures",
                expires_at=datetime.now(BEIJING_TZ) + timedelta(hours=72),
                routes_json="[]",
            )
            db.add(watch)
            db.flush()
            db.add(
                ExchangeDelistingOpportunityAlertLog(
                    watch_id=watch.id,
                    pair_key="AERGO:okx:futures:by:futures",
                    spread_pct=2.0,
                    direction="卖出OKX合约 / 买入Bybit合约",
                    status="ok",
                )
            )
            db.flush()
            snapshot = ExchangeDelistingOpportunitySnapshot(
                watch_id=watch.id,
                pair_key="AERGO:okx:futures:by:futures",
                pair_type="FF",
                symbol="AERGO",
                left_exchange="okx",
                left_market_type="futures",
                right_exchange="by",
                right_market_type="futures",
                best_executable_spread_pct=6.0,
                direction="卖出OKX合约 / 买入Bybit合约",
                status="signal_unverified",
            )

            self.assertTrue(maybe_push_opportunity_alert(db, watch, snapshot, push=True))
            self.assertFalse(maybe_push_opportunity_alert(db, watch, snapshot, push=True))
            snapshot.best_executable_spread_pct = 11.0
            self.assertTrue(maybe_push_opportunity_alert(db, watch, snapshot, push=True))
            self.assertFalse(maybe_push_opportunity_alert(db, watch, snapshot, push=True))

        self.assertEqual(send_bark.call_count, 2)
        self.assertIn("高频提醒", send_bark.call_args_list[0].kwargs["title"])
        self.assertIn("1分钟提醒", send_bark.call_args_list[1].kwargs["title"])

    def test_symbol_mute_is_persistent_and_reversible(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityMute.__table__.create(engine)
        with Session(engine) as db, patch("app.exchange_announcements.exchange_monitor_log"):
            muted = set_opportunity_symbol_mute(db, "aergo", True)
            stored = db.scalar(select(ExchangeDelistingOpportunityMute))

            self.assertEqual(muted["symbol"], "AERGO")
            self.assertTrue(muted["muted"])
            self.assertIsNotNone(stored)

            restored = set_opportunity_symbol_mute(db, "AERGO", False)
            self.assertFalse(restored["muted"])
            self.assertIsNone(db.scalar(select(ExchangeDelistingOpportunityMute)))

    def test_deleting_pair_monitor_hides_only_that_pair_persistently(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        ExchangeDelistingOpportunityPairExclusion.__table__.create(engine)
        ExchangeDelistingOpportunityMute.__table__.create(engine)
        ExchangeDelistingOpportunitySnapshot.__table__.create(engine)
        with Session(engine) as db, patch(
            "app.exchange_announcements.exchange_monitor_log"
        ):
            watch = ExchangeDelistingOpportunityWatch(
                announcement_key="aeon-delete-pair-test",
                symbol="AEON",
                source_exchange="as",
                source_market_type="futures",
                event_at=datetime.now(BEIJING_TZ),
                expires_at=datetime.now(BEIJING_TZ) + timedelta(hours=72),
                routes_json=json.dumps(
                    [
                        {
                            "exchange": "bg",
                            "market_type": "futures",
                            "symbol": "AEON",
                            "event_action": "listing",
                            "event_source": "0",
                        },
                        {
                            "exchange": "gt",
                            "market_type": "futures",
                            "symbol": "AEON",
                            "event_action": "listing",
                            "event_source": "0",
                        },
                        {
                            "exchange": "as",
                            "market_type": "futures",
                            "symbol": "AEON",
                            "event_action": "listing",
                            "event_source": "1",
                        },
                    ]
                ),
            )
            db.add(watch)
            db.flush()
            deleted_pair_key = "AEON:bg:futures:as:futures"
            retained_pair_key = "AEON:gt:futures:as:futures"
            db.add_all(
                [
                    ExchangeDelistingOpportunitySnapshot(
                        watch_id=watch.id,
                        pair_key=deleted_pair_key,
                        pair_type="FF",
                        symbol="AEON",
                        left_exchange="bg",
                        left_market_type="futures",
                        right_exchange="as",
                        right_market_type="futures",
                        status="watch",
                    ),
                    ExchangeDelistingOpportunitySnapshot(
                        watch_id=watch.id,
                        pair_key=retained_pair_key,
                        pair_type="FF",
                        symbol="AEON",
                        left_exchange="gt",
                        left_market_type="futures",
                        right_exchange="as",
                        right_market_type="futures",
                        status="watch",
                    ),
                ]
            )
            db.commit()

            result = delete_opportunity_pair_monitor(
                db,
                watch.id,
                deleted_pair_key,
            )
            overview = delisting_opportunities_overview(db)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["pair_key"], deleted_pair_key)
            self.assertEqual(
                db.scalar(
                    select(ExchangeDelistingOpportunityPairExclusion.pair_key)
                ),
                deleted_pair_key,
            )
            self.assertEqual(
                [item["pairKey"] for item in overview["items"]],
                [retained_pair_key],
            )
            self.assertEqual(overview["watch_count"], 1)

    def test_overview_hides_legacy_spot_futures_snapshot(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        ExchangeDelistingOpportunityPairExclusion.__table__.create(engine)
        ExchangeDelistingOpportunityMute.__table__.create(engine)
        ExchangeDelistingOpportunitySnapshot.__table__.create(engine)
        with Session(engine) as db:
            watch = ExchangeDelistingOpportunityWatch(
                announcement_key="aergo-legacy-sf-test",
                symbol="AERGO",
                source_exchange="by",
                source_market_type="futures",
                event_at=datetime.now(BEIJING_TZ),
                expires_at=datetime.now(BEIJING_TZ) + timedelta(hours=72),
                routes_json=json.dumps(
                    [
                        {"exchange": "okx", "market_type": "spot", "symbol": "AERGO"},
                        {"exchange": "by", "market_type": "futures", "symbol": "AERGO"},
                        {"exchange": "gt", "market_type": "futures", "symbol": "AERGO"},
                    ]
                ),
            )
            db.add(watch)
            db.flush()
            db.add_all(
                [
                    ExchangeDelistingOpportunitySnapshot(
                        watch_id=watch.id,
                        pair_key="AERGO:okx:spot:by:futures",
                        pair_type="SF",
                        symbol="AERGO",
                        left_exchange="okx",
                        left_market_type="spot",
                        right_exchange="by",
                        right_market_type="futures",
                        status="watch",
                    ),
                    ExchangeDelistingOpportunitySnapshot(
                        watch_id=watch.id,
                        pair_key="AERGO:by:futures:gt:futures",
                        pair_type="FF",
                        symbol="AERGO",
                        left_exchange="by",
                        left_market_type="futures",
                        right_exchange="gt",
                        right_market_type="futures",
                        status="watch",
                    ),
                ]
            )
            db.commit()

            overview = delisting_opportunities_overview(db)

        self.assertEqual(
            [item["pairKey"] for item in overview["items"]],
            ["AERGO:by:futures:gt:futures"],
        )
        self.assertEqual(overview["watch_count"], 1)

    def test_spot_futures_snapshot_never_pushes(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        ExchangeDelistingOpportunityMute.__table__.create(engine)
        ExchangeDelistingOpportunityAlertLog.__table__.create(engine)
        with (
            Session(engine) as db,
            patch("app.exchange_announcements.send_bark_or_log") as send_bark,
        ):
            watch = ExchangeDelistingOpportunityWatch(
                announcement_key="aergo-sf-no-push-test",
                symbol="AERGO",
                source_exchange="by",
                source_market_type="futures",
                expires_at=datetime.now(BEIJING_TZ) + timedelta(hours=72),
                routes_json="[]",
            )
            db.add(watch)
            db.flush()
            snapshot = ExchangeDelistingOpportunitySnapshot(
                watch_id=watch.id,
                pair_key="AERGO:okx:spot:by:futures",
                pair_type="SF",
                symbol="AERGO",
                left_exchange="okx",
                left_market_type="spot",
                right_exchange="by",
                right_market_type="futures",
                best_executable_spread_pct=11.0,
                direction="卖出OKX现货 / 买入Bybit合约",
                status="signal_unverified",
            )

            self.assertFalse(maybe_push_opportunity_alert(db, watch, snapshot, push=True))
            self.assertIsNone(db.scalar(select(ExchangeDelistingOpportunityAlertLog)))
            send_bark.assert_not_called()

    def test_muted_symbol_keeps_signal_but_skips_push(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        ExchangeDelistingOpportunityWatch.__table__.create(engine)
        ExchangeDelistingOpportunityMute.__table__.create(engine)
        ExchangeDelistingOpportunityAlertLog.__table__.create(engine)
        with (
            Session(engine) as db,
            patch("app.exchange_announcements.send_bark_or_log") as send_bark,
        ):
            watch = ExchangeDelistingOpportunityWatch(
                announcement_key="aergo-muted-test",
                symbol="AERGO",
                source_exchange="bn",
                source_market_type="futures",
                expires_at=datetime.now(BEIJING_TZ) + timedelta(hours=72),
                routes_json="[]",
            )
            db.add_all([watch, ExchangeDelistingOpportunityMute(symbol="AERGO")])
            db.flush()
            snapshot = ExchangeDelistingOpportunitySnapshot(
                watch_id=watch.id,
                pair_key="AERGO:okx:futures:by:futures",
                pair_type="FF",
                symbol="AERGO",
                left_exchange="okx",
                left_market_type="futures",
                right_exchange="by",
                right_market_type="futures",
                best_executable_spread_pct=11.0,
                direction="卖出OKX合约 / 买入Bybit合约",
                status="signal_unverified",
            )

            self.assertFalse(maybe_push_opportunity_alert(db, watch, snapshot, push=True))
            send_bark.assert_not_called()
            self.assertEqual(snapshot.status, "signal_unverified")


if __name__ == "__main__":
    unittest.main()
