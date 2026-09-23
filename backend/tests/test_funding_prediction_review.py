from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.funding_prediction_review import (
    funding_prediction_review_overview,
    record_prediction_checkpoint,
    reconcile_pending_predictions,
    sync_funding_formation_watches,
)
from app.funding_watch_health import FundingWatchHealth
from app.models import (
    CryptoFundingFormationPredictionLog,
    CryptoFundingFormationWatchItem,
)


def make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    CryptoFundingFormationWatchItem.__table__.create(engine)
    FundingWatchHealth.__table__.create(engine)
    CryptoFundingFormationPredictionLog.__table__.create(engine)
    return Session(engine, autoflush=False)


def prediction_payload(
    *,
    settlement_time: datetime,
    minutes_to_funding: float = 15,
    predicted_rate: float = 0.001,
) -> dict:
    return {
        "status": "ok",
        "effectiveFundingFloor": -0.02,
        "effectiveFundingCap": 0.02,
        "exchange": "bn",
        "symbol": "PROM",
        "currentFundingRate": 0.0009,
        "predictedFundingRate": predicted_rate,
        "predictedAveragePremiumRate": 0.002,
        "predictionStatus": "estimated",
        "predictionMethod": "latest_premium_carry_forward",
        "predictionModelVersion": "funding_formation_v1_latest_tick",
        "nextFundingTime": settlement_time,
        "cycleStartTime": settlement_time - timedelta(hours=4),
        "minutesToFunding": minutes_to_funding,
        "averagePremiumRate": 0.0018,
        "latestPremiumRate": 0.002,
        "coverage": 1.0,
        "weightedCoverage": 1.0,
        "formulaVersion": "bn_eight_over_n",
        "fundingIntervalHours": 4,
        "accuracyStatus": "official",
    }


def test_checkpoint_is_unique_per_cycle_and_horizon() -> None:
    db = make_session()
    settlement = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    payload = prediction_payload(settlement_time=settlement)

    assert record_prediction_checkpoint(db, payload) is not None
    db.commit()
    assert record_prediction_checkpoint(db, payload) is None
    db.commit()

    count = db.scalar(select(func.count()).select_from(CryptoFundingFormationPredictionLog))
    assert count == 1
    db.close()


def test_checkpoint_keeps_versioned_calculation_inputs_and_gate_shadow() -> None:
    db = make_session()
    settlement = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    payload = prediction_payload(settlement_time=settlement)
    payload.update(
        {
            "historyWindowStartTime": settlement - timedelta(hours=4),
            "historyWindowEndTime": settlement - timedelta(minutes=15),
            "historySource": "Gate premium history",
            "historyPrecision": "official_1m",
            "publicResolutionSeconds": 60,
            "officialSampleSeconds": 60,
            "interestRate": 0.0001,
            "intervalScale": 0.5,
            "effectiveFundingFloor": -0.003,
            "effectiveFundingCap": 0.003,
            "coveredSamples": 225,
            "elapsedSamples": 225,
            "shadowCalculation": {
                "formulaVersion": "gate_20260831_per_minute_average_shadow_v1",
                "predictedFundingRate": 0.0011,
                "coverage": 1.0,
            },
        }
    )

    row = record_prediction_checkpoint(db, payload)
    db.commit()

    assert row is not None
    details = json.loads(row.calculation_details_json or "{}")
    assert details["historySource"] == "Gate premium history"
    assert details["publicResolutionSeconds"] == 60
    assert details["fundingCap"] == 0.003
    assert details["shadowCalculation"]["formulaVersion"] == "gate_20260831_per_minute_average_shadow_v1"
    assert row.shadow_formula_version == "gate_20260831_per_minute_average_shadow_v1"
    assert row.shadow_predicted_rate == 0.0011
    db.close()


def test_twelve_minute_prediction_does_not_impersonate_fixed_checkpoint() -> None:
    db = make_session()
    settlement = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)

    assert record_prediction_checkpoint(
        db,
        prediction_payload(settlement_time=settlement, minutes_to_funding=12),
    ) is None
    db.commit()
    count = db.scalar(select(func.count()).select_from(CryptoFundingFormationPredictionLog))
    assert count == 0
    db.close()


def test_early_prediction_is_recorded_at_two_hour_checkpoint() -> None:
    db = make_session()
    settlement = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)

    row = record_prediction_checkpoint(
        db,
        prediction_payload(settlement_time=settlement, minutes_to_funding=120),
    )
    db.commit()

    assert row is not None
    assert row.checkpoint_minutes == 120
    db.close()


def test_reconciliation_matches_settlement_time_and_scores_prediction() -> None:
    db = make_session()
    settlement = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    record_prediction_checkpoint(
        db,
        prediction_payload(settlement_time=settlement, predicted_rate=0.001),
    )
    db.commit()

    def fetch_history(_exchange: str, _symbol: str, _limit: int):
        return [
            SimpleNamespace(funding_time=settlement - timedelta(hours=4), funding_rate=-0.002),
            SimpleNamespace(funding_time=settlement + timedelta(seconds=2), funding_rate=0.00105),
        ]

    result = reconcile_pending_predictions(
        db,
        fetch_history,
        now=settlement + timedelta(minutes=2),
        emit_runtime_logs=False,
    )
    row = db.scalar(select(CryptoFundingFormationPredictionLog))

    assert result["matched"] == 1
    assert row is not None
    assert row.actual_funding_rate == 0.00105
    assert row.success is True
    assert row.direction_hit is True
    assert abs((row.absolute_error or 0) - 0.00005) < 1e-12
    db.close()


def test_reconciliation_scores_shadow_formula_separately() -> None:
    db = make_session()
    settlement = datetime.now(timezone.utc) - timedelta(hours=1)
    payload = prediction_payload(settlement_time=settlement, predicted_rate=-0.001)
    payload["shadowCalculation"] = {
        "formulaVersion": "gate_20260831_per_minute_average_shadow_v1",
        "predictedFundingRate": 0.00102,
    }
    record_prediction_checkpoint(db, payload)
    db.commit()

    reconcile_pending_predictions(
        db,
        lambda *_args: [SimpleNamespace(funding_time=settlement, funding_rate=0.001)],
        now=settlement + timedelta(minutes=2),
        emit_runtime_logs=False,
    )
    overview = funding_prediction_review_overview(
        db,
        days=30,
        checkpoint_minutes=15,
        model_version="funding_formation_v1_latest_tick",
    )

    row = db.scalar(select(CryptoFundingFormationPredictionLog))
    assert row is not None
    assert row.shadow_success is True
    assert abs((row.shadow_absolute_error or 0) - 0.00002) < 1e-12
    assert overview["shadowBreakdown"][0]["hitRate"] == 1
    assert overview["shadowBreakdown"][0]["formulaVersion"] == "gate_20260831_per_minute_average_shadow_v1"
    db.close()


def test_review_uses_settled_cycles_not_refresh_count() -> None:
    db = make_session()
    settlement = datetime.now(timezone.utc) - timedelta(hours=1)
    record_prediction_checkpoint(
        db,
        prediction_payload(settlement_time=settlement, predicted_rate=-0.001),
    )
    db.commit()

    def fetch_history(_exchange: str, _symbol: str, _limit: int):
        return [SimpleNamespace(funding_time=settlement, funding_rate=0.001)]

    reconcile_pending_predictions(
        db,
        fetch_history,
        now=settlement + timedelta(minutes=2),
        emit_runtime_logs=False,
    )
    overview = funding_prediction_review_overview(
        db,
        days=30,
        checkpoint_minutes=15,
        model_version="funding_formation_v1_latest_tick",
    )

    assert overview["settledCount"] == 1
    assert overview["hitCount"] == 0
    assert overview["hitRate"] == 0
    assert overview["directionHitRate"] == 0
    assert overview["sampleStatus"] == "insufficient"
    db.close()


def test_review_success_rate_does_not_mix_model_versions() -> None:
    db = make_session()
    first_settlement = datetime.now(timezone.utc) - timedelta(hours=2)
    second_settlement = datetime.now(timezone.utc) - timedelta(hours=1)
    old_payload = prediction_payload(settlement_time=first_settlement, predicted_rate=-0.001)
    new_payload = prediction_payload(settlement_time=second_settlement, predicted_rate=0.001)
    new_payload["predictionMethod"] = "shrunk_latest_premium_carry_forward_v2"
    new_payload["predictionModelVersion"] = "funding_formation_v6_bitget_linear"
    record_prediction_checkpoint(db, old_payload)
    record_prediction_checkpoint(db, new_payload)
    db.commit()

    def fetch_history(_exchange: str, _symbol: str, _limit: int):
        return [
            SimpleNamespace(funding_time=first_settlement, funding_rate=0.001),
            SimpleNamespace(funding_time=second_settlement, funding_rate=0.001),
        ]

    reconcile_pending_predictions(
        db,
        fetch_history,
        now=second_settlement + timedelta(minutes=2),
        emit_runtime_logs=False,
    )
    overview = funding_prediction_review_overview(db, days=30, checkpoint_minutes=15)

    assert overview["modelVersion"] == "funding_formation_v6_bitget_linear"
    assert overview["settledCount"] == 1
    assert overview["hitRate"] == 1
    db.close()


def test_watch_sync_disables_routes_missing_from_full_list() -> None:
    db = make_session()
    sync_funding_formation_watches(
        db,
        [
            {"exchange": "bn", "symbol": "BTC"},
            {"exchange": "bg", "symbol": "PROM"},
        ],
    )
    result = sync_funding_formation_watches(
        db,
        [{"exchange": "bn", "symbol": "BTC"}],
    )

    assert result["itemCount"] == 1
    rows = list(db.scalars(select(CryptoFundingFormationWatchItem)))
    assert {(row.exchange, row.symbol, row.enabled) for row in rows} == {
        ("bn", "BTC", True),
        ("bg", "PROM", False),
    }
    db.close()


def test_review_scores_settlement_bounds_and_recalculates_old_matches() -> None:
    cases = [
        (0.05, 0.02, -0.01, 0.02, True, 0.0),
        (-0.06, -0.01, -0.01, 0.02, True, 0.0),
        (0.02, 0.02, -0.01, 0.02, True, 0.0),
        (-0.01, -0.01, -0.01, 0.02, True, 0.0),
        (0.05, 0.01, -0.01, 0.02, False, 0.01),
        (-0.06, -0.005, -0.01, 0.02, False, 0.005),
        (0.05, -0.01, -0.01, 0.02, False, 0.03),
        (0.001, 0.001, -0.01, 0.02, True, 0.0),
        (0.05, 0.02, None, None, False, 0.03),
        (-0.06, -0.01, None, 0.02, False, 0.05),
    ]
    for predicted, actual, floor, cap, hit, error in cases:
        db = make_session()
        settlement = datetime.now(timezone.utc) - timedelta(hours=1)
        payload = prediction_payload(settlement_time=settlement, predicted_rate=predicted)
        payload.update(effectiveFundingFloor=floor, effectiveFundingCap=cap,
                       shadowCalculation={"formulaVersion": "shadow", "predictedFundingRate": predicted})
        row = record_prediction_checkpoint(db, payload)
        db.commit()
        reconcile_pending_predictions(db, lambda *args: [SimpleNamespace(
            funding_time=settlement, funding_rate=actual)],
            now=settlement + timedelta(minutes=10), emit_runtime_logs=False)
        assert row.success is hit
        assert abs(row.absolute_error - error) < 1e-10
        assert row.shadow_success is hit
        assert abs(row.shadow_absolute_error - error) < 1e-10
        # Simulate a saved pre-fix evaluation, then ensure overview repairs all aggregates.
        row.success = False
        row.absolute_error = abs(predicted - actual)
        db.commit()
        overview = funding_prediction_review_overview(db, model_version="all")
        if floor is not None and cap is not None:
            assert overview["hitRate"] == float(hit)
            assert abs(overview["meanAbsoluteError"] - error) < 1e-10
        else:
            assert overview["hitRate"] is None
            assert overview["legacyCount"] == 1
        assert overview["items"][0]["systemPredictedRate"] == predicted
        assert row.system_predicted_rate == predicted
        db.close()
