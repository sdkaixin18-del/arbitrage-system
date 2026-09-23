from __future__ import annotations

import math
from statistics import median
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable


FUNDING_DAMPER = 0.0005
MIN_ESTIMATED_HISTORY_COVERAGE = 0.98
PREDICTION_LATEST_PREMIUM_WEIGHT = 0.85
PREDICTION_METHOD = "shrunk_latest_premium_carry_forward_v2"
PREDICTION_MODEL_VERSION = "funding_formation_v6_bitget_linear"
PREDICTION_CAP_MODE = "uncapped_observation"
BITGET_FORMULA_VERSION = "bitget_rolling_linear_5s_verified_20260913"
GATE_CONTROL_FORMULA_VERSION = "gate_legacy_eight_over_n_shadow_control"
GATE_SHADOW_FORMULA_VERSION = "gate_20260831_per_minute_average_shadow_v1"
SUPPORTED_FORMATION_EXCHANGES = {"bn", "by", "gt", "okx", "bg"}


@dataclass(frozen=True)
class FundingFormationRule:
    exchange: str
    interval_hours: float
    interest_rate: float
    interval_scale: float
    sample_seconds: int
    weighting: str
    formula_version: str
    window_mode: str = "settlement_cycle"
    aggregation_mode: str = "average_premium_then_formula"


def clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def build_cycle_premium_average_history(
    premium_rows: Iterable[dict[str, Any]],
    *,
    rule: FundingFormationRule,
    cycle_start: datetime,
    now: datetime,
) -> list[dict[str, Any]]:
    """Build the running premium mean used by the exchange's funding formula."""

    total_samples = max(1, round(rule.interval_hours * 3600 / rule.sample_seconds))
    elapsed_seconds = max(0.0, min((now - cycle_start).total_seconds(), rule.interval_hours * 3600))
    elapsed_samples = min(total_samples, max(0, math.floor(elapsed_seconds / rule.sample_seconds)))
    rows = _normalized_history_rows(
        premium_rows,
        cycle_start=cycle_start,
        rule=rule,
        elapsed_samples=elapsed_samples,
    )
    cumulative_sum = 0.0
    cumulative_weight = 0.0
    points: list[dict[str, Any]] = []
    for row in rows:
        weight = _weight_sum(row["startIndex"], row["endIndex"], rule.weighting)
        if weight <= 0:
            continue
        cumulative_sum += row["close"] * weight
        cumulative_weight += weight
        points.append(
            {
                "timestamp": min(
                    now,
                    cycle_start + timedelta(seconds=row["endIndex"] * rule.sample_seconds),
                ),
                "averagePremiumRate": cumulative_sum / cumulative_weight,
            }
        )
    return points


def funding_formation_rule(
    exchange: str,
    interval_hours: float,
    *,
    interest_rate: float | None = None,
    formula_type: str | None = None,
) -> FundingFormationRule:
    normalized = exchange.strip().lower()
    if normalized not in SUPPORTED_FORMATION_EXCHANGES:
        raise ValueError(f"unsupported funding formation exchange: {exchange}")
    if not math.isfinite(interval_hours) or interval_hours <= 0:
        raise ValueError("funding interval must be positive")

    if normalized == "by":
        # Bybit does not use the 8/N divisor. Its interest component is scaled
        # from the documented 0.03% daily rate to the active interval.
        interval_interest = (
            float(interest_rate)
            if interest_rate is not None
            else 0.0003 / (24.0 / interval_hours)
        )
        return FundingFormationRule(
            exchange=normalized,
            interval_hours=interval_hours,
            interest_rate=interval_interest,
            interval_scale=1.0,
            sample_seconds=60,
            weighting="linear",
            formula_version="bybit_interval_interest",
        )

    if normalized == "okx" and str(formula_type or "").lower() == "norate":
        # Kept for strict compatibility with any contract that has not yet
        # reported formulaType=withRate.
        interval_interest = (
            float(interest_rate)
            if interest_rate is not None
            else 0.0003 / (24.0 / interval_hours)
        )
        return FundingFormationRule(
            exchange=normalized,
            interval_hours=interval_hours,
            interest_rate=interval_interest,
            interval_scale=1.0,
            sample_seconds=60,
            weighting="linear",
            formula_version="okx_no_rate_legacy",
        )

    if normalized == "bg":
        # Since 2026-07-10 Bitget samples the premium every five seconds and
        # exposes the current estimate over a trailing N-hour window.  Public
        # history is still one-minute OHLC, so the rule is exact while the
        # historical reconstruction remains explicitly bounded/estimated.
        interval_interest = float(interest_rate) if interest_rate is not None else 0.0001
        return FundingFormationRule(
            exchange=normalized,
            interval_hours=interval_hours,
            interest_rate=interval_interest,
            interval_scale=interval_hours / 8.0,
            sample_seconds=5,
            weighting="linear",
            formula_version=BITGET_FORMULA_VERSION,
            window_mode="rolling_interval_reference",
        )

    if normalized == "gt":
        # Keep the former production result until the dedicated Gate formula
        # has accumulated settlement evidence.  The official Gate calculator
        # is emitted separately as a shadow result by gate_shadow_rule().
        interval_interest = float(interest_rate) if interest_rate is not None else 0.0001
        return FundingFormationRule(
            exchange=normalized,
            interval_hours=interval_hours,
            interest_rate=interval_interest,
            interval_scale=interval_hours / 8.0,
            sample_seconds=60,
            weighting="linear",
            formula_version=GATE_CONTROL_FORMULA_VERSION,
        )

    interval_interest = float(interest_rate) if interest_rate is not None else 0.0001
    sample_seconds = 5 if normalized == "bn" else 60
    weighting = "equal" if normalized == "bn" and math.isclose(interval_hours, 1.0) else "linear"
    formula_version = (
        "binance_eight_over_n_5s"
        if normalized == "bn"
        else "okx_with_rate_eight_over_n"
    )
    return FundingFormationRule(
        exchange=normalized,
        interval_hours=interval_hours,
        interest_rate=interval_interest,
        interval_scale=interval_hours / 8.0,
        sample_seconds=sample_seconds,
        weighting=weighting,
        formula_version=formula_version,
    )


def gate_shadow_rule(interval_hours: float) -> FundingFormationRule:
    if not math.isfinite(interval_hours) or interval_hours <= 0:
        raise ValueError("funding interval must be positive")
    return FundingFormationRule(
        exchange="gt",
        interval_hours=interval_hours,
        interest_rate=0.0003 / (24.0 / interval_hours),
        interval_scale=1.0,
        sample_seconds=60,
        weighting="equal",
        formula_version=GATE_SHADOW_FORMULA_VERSION,
        aggregation_mode="average_sample_funding",
    )


def funding_rate_from_average_premium(
    average_premium: float,
    rule: FundingFormationRule,
    *,
    floor: float | None = None,
    cap: float | None = None,
) -> float:
    pre_scaled = average_premium + clamp(
        rule.interest_rate - average_premium,
        -FUNDING_DAMPER,
        FUNDING_DAMPER,
    )
    result = pre_scaled * rule.interval_scale
    if floor is not None:
        result = max(result, floor)
    if cap is not None:
        result = min(result, cap)
    return result


def premium_boundary_for_target(
    target_rate: float,
    rule: FundingFormationRule,
    *,
    floor: float | None = None,
    cap: float | None = None,
) -> dict[str, Any]:
    if not math.isfinite(target_rate):
        return {"status": "invalid", "message": "目标资金费率不是有效数字"}
    tolerance = 1e-12
    if cap is not None and target_rate > cap + tolerance:
        return {"status": "impossible", "message": "目标高于当前资金费率上限"}
    if floor is not None and target_rate < floor - tolerance:
        return {"status": "impossible", "message": "目标低于当前资金费率下限"}

    scale = rule.interval_scale
    base_rate = rule.interest_rate * scale
    if target_rate > base_rate + tolerance:
        return {
            "status": "ok",
            "relation": "gte",
            "premiumBoundary": target_rate / scale + FUNDING_DAMPER,
            "baseFundingRate": base_rate,
        }
    if target_rate < base_rate - tolerance:
        return {
            "status": "ok",
            "relation": "lte",
            "premiumBoundary": target_rate / scale - FUNDING_DAMPER,
            "baseFundingRate": base_rate,
        }
    return {
        "status": "band",
        "relation": "band",
        "premiumBoundaryLow": rule.interest_rate - FUNDING_DAMPER,
        "premiumBoundaryHigh": rule.interest_rate + FUNDING_DAMPER,
        "baseFundingRate": base_rate,
        "message": "目标等于利率平台值，对应的是一个溢价区间，不是单一阈值",
    }


def _weight_sum(start_index: int, end_index: int, weighting: str) -> float:
    if end_index < start_index:
        return 0.0
    count = end_index - start_index + 1
    if weighting == "equal":
        return float(count)
    return (start_index + end_index) * count / 2.0


def _normalized_history_rows(
    rows: Iterable[dict[str, Any]],
    *,
    cycle_start: datetime,
    rule: FundingFormationRule,
    elapsed_samples: int,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    occupied: set[int] = set()
    for row in sorted(rows, key=lambda item: item.get("timestamp") or 0):
        timestamp = row.get("timestamp")
        if not isinstance(timestamp, datetime):
            continue
        represented = max(1, int(row.get("representedSamples") or 1))
        offset_seconds = (timestamp - cycle_start).total_seconds()
        start_index = math.floor(offset_seconds / rule.sample_seconds) + 1
        if start_index < 1:
            represented -= 1 - start_index
            start_index = 1
        if represented <= 0 or start_index > elapsed_samples:
            continue
        end_index = min(elapsed_samples, start_index + represented - 1)
        indices = [index for index in range(start_index, end_index + 1) if index not in occupied]
        if not indices:
            continue
        # API candles are aligned to one-minute buckets, so represented samples
        # are contiguous. Splitting a partially overlapping bucket protects
        # against duplicate pagination boundaries without double counting.
        ranges: list[tuple[int, int]] = []
        range_start = indices[0]
        previous = indices[0]
        for index in indices[1:]:
            if index != previous + 1:
                ranges.append((range_start, previous))
                range_start = index
            previous = index
        ranges.append((range_start, previous))
        for range_start, range_end in ranges:
            normalized.append(
                {
                    "startIndex": range_start,
                    "endIndex": range_end,
                    "close": float(row["close"]),
                    "low": float(row.get("low", row["close"])),
                    "high": float(row.get("high", row["close"])),
                }
            )
            occupied.update(range(range_start, range_end + 1))
    return normalized


def prediction_confidence(minutes_to_funding: float) -> tuple[str, str]:
    if minutes_to_funding <= 5:
        return "medium", "距结算不超过5分钟；新模型尚未完成独立长期验证，不标记高可信。"
    if minutes_to_funding <= 30:
        return "low", "距结算不超过30分钟，仍可能受最后阶段溢价变化影响。"
    if minutes_to_funding <= 60:
        return "low", "距结算超过30分钟，只作为低可信预测。"
    return "scenario", "距结算超过60分钟，未来溢价不可验证，只作为情景估算。"


def analyze_funding_formation(
    *,
    rule: FundingFormationRule,
    cycle_start: datetime,
    cycle_end: datetime,
    now: datetime,
    premium_rows: Iterable[dict[str, Any]],
    targets: Iterable[dict[str, Any]],
    floor: float | None,
    cap: float | None,
) -> dict[str, Any]:
    premium_rows = list(premium_rows)
    total_samples = max(1, round(rule.interval_hours * 3600 / rule.sample_seconds))
    elapsed_seconds = max(0.0, min((now - cycle_start).total_seconds(), (cycle_end - cycle_start).total_seconds()))
    elapsed_samples = min(total_samples, max(0, math.floor(elapsed_seconds / rule.sample_seconds)))
    normalized_rows = _normalized_history_rows(
        premium_rows,
        cycle_start=cycle_start,
        rule=rule,
        elapsed_samples=elapsed_samples,
    )

    estimated_sum = 0.0
    lower_sum = 0.0
    upper_sum = 0.0
    covered_samples = 0
    covered_weight = 0.0
    for row in normalized_rows:
        weight = _weight_sum(row["startIndex"], row["endIndex"], rule.weighting)
        estimated_sum += row["close"] * weight
        lower_sum += row["low"] * weight
        upper_sum += row["high"] * weight
        covered_weight += weight
        covered_samples += row["endIndex"] - row["startIndex"] + 1

    elapsed_weight = _weight_sum(1, elapsed_samples, rule.weighting)
    total_weight = _weight_sum(1, total_samples, rule.weighting)
    future_weight = total_weight - elapsed_weight
    coverage = 1.0 if elapsed_samples == 0 else covered_samples / elapsed_samples
    weighted_coverage = 1.0 if elapsed_weight <= 0 else covered_weight / elapsed_weight
    complete_history = coverage >= 0.999999 and weighted_coverage >= 0.999999
    usable_history = complete_history or (
        covered_weight > 0
        and coverage >= MIN_ESTIMATED_HISTORY_COVERAGE
        and weighted_coverage >= MIN_ESTIMATED_HISTORY_COVERAGE
    )
    history_status = "complete" if complete_history else "estimated" if usable_history else "insufficient"

    estimated_elapsed_sum = estimated_sum
    lower_elapsed_sum = lower_sum
    upper_elapsed_sum = upper_sum
    if usable_history and not complete_history and elapsed_weight > covered_weight:
        missing_weight = elapsed_weight - covered_weight
        estimated_elapsed_sum += estimated_sum / covered_weight * missing_weight
        lower_elapsed_sum += lower_sum / covered_weight * missing_weight
        upper_elapsed_sum += upper_sum / covered_weight * missing_weight

    average_premium = (
        estimated_elapsed_sum / elapsed_weight
        if elapsed_weight > 0 and usable_history
        else None
    )
    average_low = (
        lower_elapsed_sum / elapsed_weight
        if elapsed_weight > 0 and usable_history
        else None
    )
    average_high = (
        upper_elapsed_sum / elapsed_weight
        if elapsed_weight > 0 and usable_history
        else None
    )

    target_results: list[dict[str, Any]] = []
    for target in targets:
        target_rate = float(target["targetFundingRate"])
        boundary = premium_boundary_for_target(target_rate, rule, floor=floor, cap=cap)
        result = {
            "key": target.get("key") or "custom",
            "label": target.get("label") or "目标资金费率",
            "targetFundingRate": target_rate,
            **boundary,
        }
        if boundary.get("status") != "ok":
            target_results.append(result)
            continue
        if not usable_history:
            result.update(
                {
                    "status": "insufficient_history",
                    "message": (
                        f"本周期公开溢价历史覆盖 {coverage:.1%}、"
                        f"加权覆盖 {weighted_coverage:.1%}，低于 {MIN_ESTIMATED_HISTORY_COVERAGE:.0%}，"
                        "暂不反推剩余阈值"
                    ),
                }
            )
            target_results.append(result)
            continue
        if future_weight <= 0:
            current_final = funding_rate_from_average_premium(
                average_premium or 0.0,
                rule,
                floor=floor,
                cap=cap,
            )
            relation = boundary["relation"]
            reached = current_final >= target_rate if relation == "gte" else current_final <= target_rate
            result.update(
                {
                    "status": "settled",
                    "reached": reached,
                    "calculatedFundingRate": current_final,
                    "message": "本周期已结束，没有剩余采样时间",
                }
            )
            target_results.append(result)
            continue

        premium_boundary = float(boundary["premiumBoundary"])
        estimated_required = (premium_boundary * total_weight - estimated_elapsed_sum) / future_weight
        lower_required = (premium_boundary * total_weight - upper_elapsed_sum) / future_weight
        upper_required = (premium_boundary * total_weight - lower_elapsed_sum) / future_weight
        relation = boundary["relation"]
        conservative_required = upper_required if relation == "gte" else lower_required
        result.update(
            {
                "estimatedRequiredPremiumRate": estimated_required,
                "requiredPremiumRate": conservative_required,
                "requiredPremiumRangeLow": min(lower_required, upper_required),
                "requiredPremiumRangeHigh": max(lower_required, upper_required),
                "conservative": not math.isclose(lower_sum, upper_sum, rel_tol=0.0, abs_tol=1e-15),
            }
        )
        if not complete_history:
            result.update(
                {
                    "status": "estimated",
                    "message": (
                        f"历史覆盖 {coverage:.1%}，少量缺口按已覆盖样本的加权均值估算"
                    ),
                }
            )
        target_results.append(result)

    calculated_rate = (
        funding_rate_from_average_premium(average_premium, rule, floor=floor, cap=cap)
        if average_premium is not None
        else None
    )
    latest_premium = normalized_rows[-1]["close"] if normalized_rows else None
    # One vote per completed minute: neither an unfinished candle nor duplicate
    # pagination rows should dominate the forward scenario. Keep genuine zeros.
    recent_by_minute: dict[datetime, dict[str, Any]] = {}
    for row in sorted(premium_rows, key=lambda r: r["timestamp"]):
        timestamp = row["timestamp"]
        minute = timestamp.replace(second=0, microsecond=0)
        if (cycle_start <= timestamp and now - timedelta(minutes=6) < minute
                and minute + timedelta(minutes=1) <= now):
            if all(math.isfinite(float(row.get(k, row["close"]))) for k in ("close", "low", "high")):
                recent_by_minute[minute] = row
    recent_rows = list(recent_by_minute.values())[-5:]
    recent_median = median(float(r["close"]) for r in recent_rows) if recent_rows else None
    recent_age = ((now - max(recent_by_minute) - timedelta(minutes=1)).total_seconds()
                  if recent_by_minute else None)
    recent_usable = len(recent_rows) >= 3 and recent_age is not None and recent_age <= 120
    predicted_average_premium: float | None = None
    predicted_funding_rate: float | None = None
    future_premium_anchor: float | None = None
    sensitivity_low: float | None = None
    sensitivity_high: float | None = None
    robust_predicted_rate: float | None = None
    minutes_to_funding = max(0.0, (cycle_end - now).total_seconds() / 60)
    confidence, confidence_message = prediction_confidence(minutes_to_funding)
    if usable_history and total_weight > 0 and latest_premium is not None and (recent_usable or future_weight <= 0):
        future_premium_anchor = (
            PREDICTION_LATEST_PREMIUM_WEIGHT * latest_premium
            + (1.0 - PREDICTION_LATEST_PREMIUM_WEIGHT) * average_premium
        )
        predicted_average_premium = (
            estimated_elapsed_sum + future_premium_anchor * future_weight
        ) / total_weight
        predicted_funding_rate = funding_rate_from_average_premium(
            predicted_average_premium,
            rule,
            floor=None,
            # Observation predictions bypass both settlement bounds.
            cap=None,
        )
        # Fixed median challenger underperformed the held-out replay. Record it
        # for comparison without silently promoting it to the primary forecast.
        if recent_median is not None:
            robust_anchor = PREDICTION_LATEST_PREMIUM_WEIGHT * recent_median + (1.0 - PREDICTION_LATEST_PREMIUM_WEIGHT) * average_premium
            robust_predicted_rate = funding_rate_from_average_premium(
                (estimated_elapsed_sum + robust_anchor * future_weight) / total_weight, rule, floor=None, cap=None)
        # Stress scenarios, not a calibrated probability/confidence interval.
        recent_low = min([latest_premium, future_premium_anchor] + [float(r.get("low", r["close"])) for r in recent_rows])
        recent_high = max([latest_premium, future_premium_anchor] + [float(r.get("high", r["close"])) for r in recent_rows])
        sensitivity_low = funding_rate_from_average_premium(
            (lower_elapsed_sum + recent_low * future_weight) / total_weight, rule, floor=None, cap=None)
        sensitivity_high = funding_rate_from_average_premium(
            (upper_elapsed_sum + recent_high * future_weight) / total_weight, rule, floor=None, cap=None)
        if future_weight > 0 and sensitivity_high - sensitivity_low > 0.001:
            confidence = "scenario" if minutes_to_funding > 60 else "low"
            confidence_message += " 近期波动或分钟采样误差较大，情景范围较宽。"
        prediction_status = "settled" if future_weight <= 0 else "estimated"
        prediction_message = (
            "本周期已结束，结果按完整周期公开溢价样本计算。"
            if future_weight <= 0
            else (
                "未来溢价按最新分钟收盘值85%与本周期均值15%组合外推；五分钟中位数仅作对照，"
                f"再按交易所权重计算；{confidence_message}"
            )
        )
        prediction_message += " 预测资金费不应用交易所上限或下限，仅用于观察；实际结算仍受上下限约束。"
    else:
        prediction_status = "insufficient"
        confidence = "low"
        prediction_message = "本周期历史不足，或最近完整分钟样本少于3个/已过期，暂不生成预测资金费。"
    return {
        "totalSamples": total_samples,
        "elapsedSamples": elapsed_samples,
        "remainingSamples": total_samples - elapsed_samples,
        "coveredSamples": covered_samples,
        "coverage": coverage,
        "weightedCoverage": weighted_coverage,
        "completeHistory": complete_history,
        "historyStatus": history_status,
        "averagePremiumRate": average_premium,
        "averagePremiumLow": average_low,
        "averagePremiumHigh": average_high,
        "calculatedFundingRate": calculated_rate,
        "predictedAveragePremiumRate": predicted_average_premium,
        "predictedFundingRate": predicted_funding_rate,
        "futurePremiumAnchorRate": future_premium_anchor,
        "recentPremiumMedianRate": recent_median,
        "robustPredictedFundingRate": robust_predicted_rate,
        "recentPremiumSampleCount": len(recent_rows),
        "recentPremiumAgeSeconds": recent_age,
        "predictionSensitivityLow": sensitivity_low,
        "predictionSensitivityHigh": sensitivity_high,
        "predictionStatus": prediction_status,
        "predictionMethod": PREDICTION_METHOD,
        "predictionModelVersion": PREDICTION_MODEL_VERSION,
        "predictionCapMode": PREDICTION_CAP_MODE,
        "predictionCapApplied": False,
        "predictionConfidence": "settled" if future_weight <= 0 else confidence,
        "predictionLatestPremiumWeight": PREDICTION_LATEST_PREMIUM_WEIGHT,
        "predictionMessage": prediction_message,
        "targets": target_results,
    }


def analyze_rolling_reference(
    *,
    rule: FundingFormationRule,
    window_start: datetime,
    window_end: datetime,
    premium_rows: Iterable[dict[str, Any]],
    floor: float | None,
    cap: float | None,
) -> dict[str, Any]:
    """Rebuild an exchange's trailing-window current estimate.

    This is deliberately separate from the next-settlement forecast. Samples
    before the active settlement cycle help reproduce Bitget's *current*
    rolling estimate, but they will roll out before the next settlement and
    therefore must not be counted as already-formed final-cycle samples.
    """

    calculation = analyze_funding_formation(
        rule=rule,
        cycle_start=window_start,
        cycle_end=window_end,
        now=window_end,
        premium_rows=premium_rows,
        targets=[],
        floor=floor,
        cap=cap,
    )
    return {
        "windowStartTime": window_start,
        "windowEndTime": window_end,
        "sampleCount": calculation["coveredSamples"],
        "expectedSampleCount": calculation["totalSamples"],
        "coverage": calculation["coverage"],
        "averagePremiumRate": calculation["averagePremiumRate"],
        "calculatedFundingRate": calculation["calculatedFundingRate"],
        "historyStatus": calculation["historyStatus"],
    }


def _gate_sample_funding(premium: float, rule: FundingFormationRule) -> float:
    return premium + clamp(
        rule.interest_rate - premium,
        -FUNDING_DAMPER,
        FUNDING_DAMPER,
    )


def analyze_gate_shadow(
    *,
    rule: FundingFormationRule,
    cycle_start: datetime,
    cycle_end: datetime,
    now: datetime,
    premium_rows: Iterable[dict[str, Any]],
    floor: float | None,
    cap: float | None,
) -> dict[str, Any]:
    """Calculate Gate's official per-minute formula without replacing prod.

    Gate applies the dampener to each minute's premium/interest result and
    then takes the arithmetic mean for the cycle.  Applying the dampener once
    to an already averaged premium is not equivalent when samples cross the
    interest plateau, so this shadow calculator keeps the operations ordered.
    """

    total_samples = max(1, round(rule.interval_hours * 3600 / rule.sample_seconds))
    elapsed_seconds = max(
        0.0,
        min((now - cycle_start).total_seconds(), (cycle_end - cycle_start).total_seconds()),
    )
    elapsed_samples = min(total_samples, max(0, math.floor(elapsed_seconds / rule.sample_seconds)))
    normalized_rows = _normalized_history_rows(
        premium_rows,
        cycle_start=cycle_start,
        rule=rule,
        elapsed_samples=elapsed_samples,
    )
    covered_samples = 0
    premium_sum = 0.0
    sample_funding_sum = 0.0
    for row in normalized_rows:
        count = row["endIndex"] - row["startIndex"] + 1
        covered_samples += count
        premium_sum += row["close"] * count
        sample_funding_sum += _gate_sample_funding(row["close"], rule) * count

    coverage = 1.0 if elapsed_samples == 0 else covered_samples / elapsed_samples
    usable = covered_samples > 0 and coverage >= MIN_ESTIMATED_HISTORY_COVERAGE
    if usable and covered_samples < elapsed_samples:
        missing = elapsed_samples - covered_samples
        premium_sum += premium_sum / covered_samples * missing
        sample_funding_sum += sample_funding_sum / covered_samples * missing

    average_premium = premium_sum / elapsed_samples if usable and elapsed_samples else None
    calculated_rate = sample_funding_sum / elapsed_samples if usable and elapsed_samples else None
    if calculated_rate is not None:
        calculated_rate = clamp(
            calculated_rate,
            floor if floor is not None else -math.inf,
            cap if cap is not None else math.inf,
        )

    latest_premium = normalized_rows[-1]["close"] if normalized_rows else None
    predicted_rate: float | None = None
    predicted_average_premium: float | None = None
    future_anchor: float | None = None
    remaining_samples = total_samples - elapsed_samples
    if usable and average_premium is not None and latest_premium is not None:
        future_anchor = (
            PREDICTION_LATEST_PREMIUM_WEIGHT * latest_premium
            + (1.0 - PREDICTION_LATEST_PREMIUM_WEIGHT) * average_premium
        )
        predicted_average_premium = (
            premium_sum + future_anchor * remaining_samples
        ) / total_samples
        predicted_rate = (
            sample_funding_sum
            + _gate_sample_funding(future_anchor, rule) * remaining_samples
        ) / total_samples

    return {
        "status": "estimated" if usable else "insufficient",
        "formulaVersion": rule.formula_version,
        "aggregationMode": rule.aggregation_mode,
        "sampleSeconds": rule.sample_seconds,
        "interestRate": rule.interest_rate,
        "intervalScale": rule.interval_scale,
        "windowStartTime": cycle_start,
        "windowEndTime": now,
        "totalSamples": total_samples,
        "elapsedSamples": elapsed_samples,
        "remainingSamples": remaining_samples,
        "coveredSamples": covered_samples,
        "coverage": coverage,
        "averagePremiumRate": average_premium,
        "calculatedFundingRate": calculated_rate,
        "predictedAveragePremiumRate": predicted_average_premium,
        "predictedFundingRate": predicted_rate,
        "predictionCapMode": PREDICTION_CAP_MODE,
        "predictionCapApplied": False,
        "futurePremiumAnchorRate": future_anchor,
    }
