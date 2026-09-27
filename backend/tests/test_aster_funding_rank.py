from datetime import datetime, timedelta, timezone

import pytest

from app import aster_funding_rank as rank
from app.database import SessionLocal, engine
from app.models import AsterFundingRankSnapshot


def cutoff() -> datetime:
    return datetime(2026, 9, 22, 16, tzinfo=timezone.utc)  # Beijing 00:00


def market(symbol: str = "ABCUSDT") -> dict:
    return {"symbol": symbol, "baseAsset": symbol.removesuffix("USDT"),
            "onboardDate": rank._utc_ms(cutoff() - timedelta(days=30))}


def entry(hours_before: int, rate: str, offset_ms: int = 0) -> dict:
    return {"symbol": "ABCUSDT", "fundingTime": rank._utc_ms(cutoff() - timedelta(hours=hours_before)) + offset_ms,
            "fundingRate": rate}


ADDRESS = [{"chainId": "56", "address": "0x" + "1" * 40, "source": "Astro 已配置"}]


def test_beijing_cutoffs_and_signed_24h_window() -> None:
    assert rank.latest_cutoff(datetime(2026, 9, 22, 16, 5, tzinfo=timezone.utc)) == cutoff()
    assert rank.latest_cutoff(datetime(2026, 9, 22, 19, 59, tzinfo=timezone.utc)) == cutoff()
    assert rank.latest_cutoff(datetime(2026, 9, 22, 20, tzinfo=timezone.utc)) == cutoff()
    assert rank.latest_cutoff(datetime(2026, 9, 23, 0, tzinfo=timezone.utc)) == cutoff() + timedelta(hours=8)
    assert rank.latest_cutoff(datetime(2026, 9, 23, 7, 59, tzinfo=timezone.utc)) == cutoff() + timedelta(hours=8)
    assert rank.latest_cutoff(datetime(2026, 9, 23, 8, tzinfo=timezone.utc)) == cutoff() + timedelta(hours=16)
    history = [entry(24, "0.1", 1), entry(16, "0.0003"), entry(8, "-0.0001"), entry(0, "0.0002", 1)]
    result = rank.rank_symbol(market(), history, 8, rank._utc_ms(cutoff()), ADDRESS)
    assert result is not None
    assert result["settlementCount"] == 3
    assert result["totalRatePct"] == pytest.approx(0.04)


def test_duplicate_and_missing_settlements_do_not_inflate_rank() -> None:
    history = [entry(16, "0.0003"), entry(16, "0.0003"), entry(8, "-0.0001"), entry(0, "0.0002")]
    result = rank.rank_symbol(market(), history, 8, rank._utc_ms(cutoff()), ADDRESS)
    assert result is not None and result["settlementCount"] == 3
    with pytest.raises(ValueError, match="冲突"):
        rank.rank_symbol(market(), history + [entry(16, "0.9")], 8, rank._utc_ms(cutoff()), ADDRESS)
    with pytest.raises(ValueError, match="缺口"):
        rank.rank_symbol(market(), [entry(16, "0.0003"), entry(0, "0.0002")], 8,
                         rank._utc_ms(cutoff()), ADDRESS)
    assert rank.rank_symbol(market(), [entry(16, "-0.0003"), entry(8, "0.0001"), entry(0, "0")],
                            8, rank._utc_ms(cutoff()), ADDRESS) is None


def test_only_existing_valid_chain_addresses_are_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import astro_spread_scanner

    monkeypatch.setattr(astro_spread_scanner, "spread_scan_dex_mapped_assets", lambda: [
        {"symbol": "ABC", "chainIndex": "56", "contractAddress": "0x" + "1" * 40},
        {"symbol": "BAD", "chainIndex": "56", "contractAddress": "invalid"},
        {"symbol": "CONFLICT", "chainIndex": "1", "contractAddress": "0x" + "2" * 40},
        {"symbol": "CONFLICT", "chainIndex": "1", "contractAddress": "0x" + "3" * 40},
    ])
    monkeypatch.setattr(rank, "load_asset_alias_config", lambda: {"assets": []})
    mapping = rank.mapped_addresses()
    assert list(mapping) == ["ABC"]
    assert mapping["ABC"][0]["address"] == "0x" + "1" * 40


def test_full_market_history_uses_24_non_overlapping_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    cutoff_ms = rank._utc_ms(cutoff())
    windows: list[tuple[int, int]] = []

    def fake_hour(start_ms: int, end_ms: int) -> list[dict]:
        windows.append((start_ms, end_ms))
        return [{"symbol": "ABCUSDT", "fundingTime": end_ms, "fundingRate": "0.0001"}]

    monkeypatch.setattr(rank, "fetch_hour_history", fake_hour)
    history = rank.fetch_all_history(cutoff_ms)
    assert sorted(windows) == [(cutoff_ms - rank.DAY_MS + hour * rank.HOUR_MS,
                                cutoff_ms - rank.DAY_MS + (hour + 1) * rank.HOUR_MS)
                               for hour in range(24)]
    assert len(history["ABCUSDT"]) == 24


def test_quote_volume_matches_cutoff_and_rejects_incomplete_klines() -> None:
    end_ms = rank._utc_ms(cutoff())
    start_ms = end_ms - rank.DAY_MS
    candles = [[start_ms + hour * rank.HOUR_MS, "0", "0", "0", "0", "0",
                start_ms + (hour + 1) * rank.HOUR_MS - 1, str(hour + 1)]
               for hour in range(24)]
    assert rank.quote_volume_from_klines("ABCUSDT", candles, end_ms, start_ms - 1) == 300
    with pytest.raises(ValueError, match="不完整"):
        rank.quote_volume_from_klines("ABCUSDT", candles[:-1], end_ms, start_ms - 1)
    with pytest.raises(ValueError, match="重复"):
        rank.quote_volume_from_klines("ABCUSDT", candles + [candles[-1]], end_ms, start_ms - 1)
    with pytest.raises(ValueError, match="数值无效"):
        rank.quote_volume_from_klines("ABCUSDT", [*candles[:-1], [*candles[-1][:7], "-1"]], end_ms, start_ms - 1)
    assert rank.quote_volume_from_klines("ABCUSDT", candles[12:], end_ms, start_ms + 12 * rank.HOUR_MS) == 222


def test_snapshot_is_atomic_and_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    AsterFundingRankSnapshot.__table__.create(bind=engine, checkfirst=True)
    monkeypatch.setattr(rank, "mapped_addresses", lambda: {"ABC": ADDRESS})
    monkeypatch.setattr(rank, "fetch_market_data", lambda: ([market(), market("OTHERUSDT")], {"ABCUSDT": 8}))
    monkeypatch.setattr(rank, "fetch_all_history", lambda cutoff_ms: {"ABCUSDT": [
        entry(16, "0.0003"), entry(8, "-0.0001"), entry(0, "0.0002")]})
    monkeypatch.setattr(rank, "fetch_quote_volume", lambda symbol, cutoff_ms, onboard_ms: 12345.67)
    first = rank.generate_snapshot(cutoff())
    second = rank.generate_snapshot(cutoff())
    assert first["eligibleCount"] == second["eligibleCount"] == 1
    with SessionLocal() as db:
        assert db.query(AsterFundingRankSnapshot).filter_by(as_of_ms=rank._utc_ms(cutoff())).count() == 1
        response = rank.get_aster_funding_rank(20, db)
    assert response["rows"][0]["coin"] == "ABC"
    assert response["rows"][0]["addresses"] == ADDRESS
    assert response["rows"][0]["volume24hUsdt"] == 12345.67


def test_incomplete_market_does_not_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    AsterFundingRankSnapshot.__table__.create(bind=engine, checkfirst=True)
    later = cutoff() + timedelta(hours=8)
    monkeypatch.setattr(rank, "mapped_addresses", lambda: {"ABC": ADDRESS})
    monkeypatch.setattr(rank, "fetch_market_data", lambda: ([market()], {"ABCUSDT": 8}))
    monkeypatch.setattr(rank, "fetch_all_history", lambda cutoff_ms: {})
    with pytest.raises(ValueError, match="无法核验"):
        rank.generate_snapshot(later)
    with SessionLocal() as db:
        assert db.query(AsterFundingRankSnapshot).filter_by(as_of_ms=rank._utc_ms(later)).count() == 0
