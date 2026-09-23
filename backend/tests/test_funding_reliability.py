from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.funding_formation import analyze_funding_formation, funding_formation_rule
from app.crypto import fetch_funding_premium_history, stale_funding_formation_payload

START = datetime(2026, 9, 13, tzinfo=timezone.utc)


def analyze(values, *, now_minute=30, missing_tail=0):
    rows = [dict(timestamp=START + timedelta(minutes=i), close=v, low=v, high=v, representedSamples=12)
            for i, v in enumerate(values)]
    if missing_tail:
        rows = rows[:-missing_tail]
    return analyze_funding_formation(rule=funding_formation_rule('bn', 4), cycle_start=START,
                                    cycle_end=START + timedelta(hours=4), now=START + timedelta(minutes=now_minute),
                                    premium_rows=rows, targets=[], floor=-0.02, cap=0.02)


def test_challenger_ignores_unfinished_spike_but_primary_retains_valid_latest():
    r = analyze([-0.01] * 29 + [-0.04, 0.5])
    assert r['recentPremiumMedianRate'] == -0.01
    assert r['recentPremiumSampleCount'] == 5
    assert r['predictedFundingRate'] < r['robustPredictedFundingRate']
    assert r['predictionSensitivityLow'] <= r['predictedFundingRate'] <= r['predictionSensitivityHigh']
    assert r['predictionConfidence'] == 'scenario'


def test_real_zeros_are_preserved_and_persistent_change_reaches_challenger():
    r = analyze([-0.01] * 25 + [0.0] * 5)
    assert r['recentPremiumMedianRate'] == 0.0
    assert r['robustPredictedFundingRate'] == r['predictedFundingRate']


def test_recent_gap_suppresses_forecast_even_with_good_overall_coverage():
    r = analyze([-0.01] * 200, now_minute=200, missing_tail=3)
    assert r['coverage'] >= 0.98
    assert r['predictedFundingRate'] is None
    assert r['predictionStatus'] == 'insufficient'


def test_wide_range_downgrades_near_settlement_confidence():
    r = analyze([-0.01] * 231 + [-0.1, 0.1, -0.1, 0.1], now_minute=235)
    assert r['predictionConfidence'] == 'low'
    assert r['predictionSensitivityHigh'] > r['predictionSensitivityLow']


def test_bitget_exclusive_start_pages_do_not_skip_minutes():
    calls = []
    def request(_client, _url, params):
        calls.append(params)
        start, end = params['startTime'], params['endTime']
        return {'code': '00000', 'data': [[str(t), '-0.01', '0', '-0.02', '0']
                for t in range(int(START.timestamp()*1000), int((START+timedelta(minutes=242)).timestamp()*1000), 60000)
                if start < t <= end]}
    with patch('app.crypto.request_json', side_effect=request):
        rows, _ = fetch_funding_premium_history(None, 'bg', 'LSK', START+timedelta(seconds=17), START+timedelta(minutes=240, seconds=17))
    assert len(rows) == 241
    assert len({r['timestamp'] for r in rows}) == 241
    assert all(r['close'] == 0.0 for r in rows)
    assert all(c['limit'] == 100 for c in calls)
    assert len(calls) == 3


def test_failed_refresh_never_presents_old_forecast_as_current():
    payload = {'predictedFundingRate': -0.01, 'predictedAveragePremiumRate': -0.02,
               'robustPredictedFundingRate': -0.009, 'predictionSensitivityLow': -0.02,
               'predictionSensitivityHigh': 0.0, 'latestPremiumRate': 0.0}
    result = stale_funding_formation_payload((START, payload), now=START+timedelta(minutes=2), error='timeout')
    assert result['stale'] is True
    assert result['predictedFundingRate'] is None
    assert result['robustPredictedFundingRate'] is None
    assert result['predictionSensitivityLow'] is None
    assert result['latestPremiumRate'] == 0.0
    assert payload['predictedFundingRate'] == -0.01


def test_bitget_official_linear_weights_for_two_minutes():
    # 12 samples/minute: weights 1..12 sum to 78, 13..24 to 222.
    # A later -3% minute must outweigh an earlier -1% minute.
    rows = [dict(timestamp=START, close=-0.01, representedSamples=12),
            dict(timestamp=START+timedelta(minutes=1), close=-0.03, representedSamples=12)]
    r = analyze_funding_formation(rule=funding_formation_rule('bg', 4), cycle_start=START,
                                 cycle_end=START+timedelta(hours=4), now=START+timedelta(minutes=2),
                                 premium_rows=rows, targets=[], floor=-0.02, cap=0.02)
    assert abs(r['averagePremiumRate'] - (-0.0248)) < 1e-12
    assert abs(r['calculatedFundingRate'] - (-0.01215)) < 1e-12


def test_bitget_rolling_reference_retains_linear_weighting():
    from app.funding_formation import analyze_rolling_reference
    rows = [dict(timestamp=START+timedelta(minutes=i), close=(-0.01 if i<120 else -0.03), representedSamples=12)
            for i in range(240)]
    r = analyze_rolling_reference(rule=funding_formation_rule('bg', 4), window_start=START,
                                 window_end=START+timedelta(hours=4), premium_rows=rows, floor=None, cap=None)
    assert r['averagePremiumRate'] < -0.0249
    assert r['averagePremiumRate'] > -0.0251
    assert r['coverage'] == 1.0
