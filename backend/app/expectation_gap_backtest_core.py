from __future__ import annotations

import hashlib
import math
import random
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence


HORIZONS = (5, 20, 60)
DECISION_CODES = ("A", "B", "C", "D")


@dataclass(frozen=True)
class GapThresholds:
    """Frozen market-specific thresholds selected only on the development sample."""

    evidence_growth_pct: float
    evidence_acceleration_pct: float
    unpriced_20d_excess_pct: float
    priced_20d_excess_pct: float
    priced_60d_excess_pct: float
    confirmation_excess_pct: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


DEFAULT_THRESHOLDS: dict[str, GapThresholds] = {
    "A股": GapThresholds(30.0, 0.0, 3.0, 15.0, 25.0, 0.0),
    "美股": GapThresholds(20.0, 0.0, 3.0, 15.0, 25.0, 0.0),
}


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def stable_rank(value: str, salt: str = "expectation-gap-v1") -> str:
    return hashlib.sha256(f"{salt}|{value}".encode("utf-8")).hexdigest()


def stable_sample(values: Iterable[str], limit: int, salt: str = "expectation-gap-v1") -> list[str]:
    unique = sorted({str(value).strip() for value in values if str(value).strip()})
    if limit <= 0 or len(unique) <= limit:
        return unique
    return sorted(unique, key=lambda value: stable_rank(value, salt))[:limit]


def median(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(clean) if clean else None


def mean(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return sum(clean) / len(clean) if clean else None


def percentile(values: Iterable[float | None], quantile: float) -> float | None:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = max(0.0, min(1.0, quantile)) * (len(clean) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return clean[lower]
    fraction = position - lower
    return clean[lower] * (1 - fraction) + clean[upper] * fraction


def _bar_date(row: Mapping[str, Any]) -> date:
    value = row.get("trade_date")
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _sorted_bars(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = [dict(row) for row in rows]
    for row in result:
        row["trade_date"] = _bar_date(row)
    return sorted(result, key=lambda row: row["trade_date"])


def price_limit_pct(code: str, name: str | None = None) -> float:
    """Conservative A-share daily price-limit proxy for tradability checks."""

    digits = "".join(character for character in str(code) if character.isdigit())[-6:]
    label = (name or "").upper().replace(" ", "")
    if "ST" in label:
        return 5.0
    if digits.startswith(("300", "301", "688", "689")):
        return 20.0
    if digits.startswith(("4", "8", "9")):
        return 30.0
    return 10.0


def next_tradable_entry(
    bars: Sequence[Mapping[str, Any]],
    signal_date: date,
    market: str,
    code: str,
    name: str | None = None,
    max_defer_sessions: int = 5,
) -> dict[str, Any]:
    """Return a conservative next-session entry without pretending locked limit-ups were fillable."""

    ordered = _sorted_bars(bars)
    first_index = next((index for index, row in enumerate(ordered) if row["trade_date"] > signal_date), None)
    if first_index is None:
        return {"available": False, "reason": "no_post_signal_bar", "deferred_sessions": 0}

    deferred = 0
    reasons: list[str] = []
    for index in range(first_index, min(len(ordered), first_index + max_defer_sessions + 1)):
        row = ordered[index]
        open_value = safe_float(row.get("open"))
        volume_value = safe_float(row.get("volume"))
        amount_value = safe_float(row.get("amount"))
        if not open_value or open_value <= 0:
            reasons.append("invalid_open")
            deferred += 1
            continue
        if (volume_value is not None and volume_value <= 0) or (amount_value is not None and amount_value <= 0):
            reasons.append("suspended_or_no_volume")
            deferred += 1
            continue
        if market == "A股":
            if index == 0:
                reasons.append("missing_previous_close")
                deferred += 1
                continue
            previous_close = safe_float(ordered[index - 1].get("close"))
            if not previous_close or previous_close <= 0:
                reasons.append("missing_previous_close")
                deferred += 1
                continue
            open_gap_pct = (open_value / previous_close - 1) * 100
            limit_pct = price_limit_pct(code, name)
            # Rounding and adjusted-price noise justify a small tolerance.
            if open_gap_pct >= limit_pct - 0.35:
                reasons.append("open_at_or_near_limit_up")
                deferred += 1
                continue
        return {
            "available": True,
            "entry_index": index,
            "entry_date": row["trade_date"].isoformat(),
            "entry_price": open_value,
            "deferred_sessions": deferred,
            "defer_reasons": reasons,
        }
    return {
        "available": False,
        "reason": "not_tradable_within_defer_window",
        "deferred_sessions": deferred,
        "defer_reasons": reasons,
    }


def return_between_dates(
    bars: Sequence[Mapping[str, Any]],
    entry_date: date,
    exit_date: date,
) -> float | None:
    ordered = _sorted_bars(bars)
    entry = next((row for row in ordered if row["trade_date"] == entry_date), None)
    exit_row = next((row for row in ordered if row["trade_date"] == exit_date), None)
    entry_open = safe_float(entry.get("open")) if entry else None
    exit_close = safe_float(exit_row.get("close")) if exit_row else None
    if not entry_open or entry_open <= 0 or exit_close is None:
        return None
    return (exit_close / entry_open - 1) * 100


def same_session_return(bars: Sequence[Mapping[str, Any]], trade_date: date) -> float | None:
    row = next((item for item in _sorted_bars(bars) if item["trade_date"] == trade_date), None)
    open_value = safe_float(row.get("open")) if row else None
    close_value = safe_float(row.get("close")) if row else None
    if not open_value or close_value is None:
        return None
    return (close_value / open_value - 1) * 100


def market_confirmation(
    bars: Sequence[Mapping[str, Any]],
    benchmark_bars: Sequence[Mapping[str, Any]],
    observation_date: date,
) -> dict[str, float | None]:
    stock_return = same_session_return(bars, observation_date)
    benchmark_return = same_session_return(benchmark_bars, observation_date)
    return {
        "confirmation_return_pct": stock_return,
        "confirmation_benchmark_return_pct": benchmark_return,
        "market_confirmation_excess_pct": (
            stock_return - benchmark_return
            if stock_return is not None and benchmark_return is not None
            else None
        ),
    }


def attach_horizon_returns(
    bars: Sequence[Mapping[str, Any]],
    entry: Mapping[str, Any],
    benchmark_bars: Sequence[Mapping[str, Any]],
    cost_bps: int,
) -> dict[str, Any]:
    ordered = _sorted_bars(bars)
    entry_index = int(entry["entry_index"])
    entry_price = float(entry["entry_price"])
    cost_pct = cost_bps / 100.0
    output: dict[str, Any] = {}
    for horizon in HORIZONS:
        exit_index = entry_index + horizon - 1
        if exit_index >= len(ordered):
            output[str(horizon)] = {"available": False, "bars_available": len(ordered) - entry_index}
            continue
        window = ordered[entry_index : exit_index + 1]
        exit_bar = ordered[exit_index]
        exit_close = safe_float(exit_bar.get("close"))
        highs = [safe_float(row.get("high")) for row in window]
        lows = [safe_float(row.get("low")) for row in window]
        if exit_close is None or not all(value is not None for value in highs + lows):
            output[str(horizon)] = {"available": False, "bars_available": len(window), "reason": "invalid_bar"}
            continue
        gross_return = (exit_close / entry_price - 1) * 100
        net_return = gross_return - cost_pct
        benchmark_return = return_between_dates(
            benchmark_bars,
            date.fromisoformat(str(entry["entry_date"])),
            exit_bar["trade_date"],
        )
        output[str(horizon)] = {
            "available": True,
            "exit_date": exit_bar["trade_date"].isoformat(),
            "exit_price": round(exit_close, 6),
            "gross_return_pct": round(gross_return, 6),
            "return_pct": round(net_return, 6),
            "benchmark_return_pct": round(benchmark_return, 6) if benchmark_return is not None else None,
            "market_excess_return_pct": round(net_return - benchmark_return, 6) if benchmark_return is not None else None,
            "mfe_pct": round((max(float(value) for value in highs) / entry_price - 1) * 100 - cost_pct, 6),
            "mae_pct": round((min(float(value) for value in lows) / entry_price - 1) * 100 - cost_pct, 6),
        }
    return output


def pre_event_pricing(
    bars: Sequence[Mapping[str, Any]],
    benchmark_bars: Sequence[Mapping[str, Any]],
    signal_date: date,
) -> dict[str, float | None]:
    ordered = [row for row in _sorted_bars(bars) if row["trade_date"] < signal_date]
    benchmark = _sorted_bars(benchmark_bars)
    if len(ordered) < 21:
        return {"pre_20d_excess_pct": None, "pre_60d_excess_pct": None, "distance_to_60d_high_pct": None}

    def period_excess(period: int) -> float | None:
        if len(ordered) <= period:
            return None
        start = ordered[-period - 1]
        end = ordered[-1]
        start_close = safe_float(start.get("close"))
        end_close = safe_float(end.get("close"))
        if not start_close or end_close is None:
            return None
        stock_return = (end_close / start_close - 1) * 100
        benchmark_start = next((row for row in benchmark if row["trade_date"] == start["trade_date"]), None)
        benchmark_end = next((row for row in benchmark if row["trade_date"] == end["trade_date"]), None)
        start_benchmark = safe_float(benchmark_start.get("close")) if benchmark_start else None
        end_benchmark = safe_float(benchmark_end.get("close")) if benchmark_end else None
        if not start_benchmark or end_benchmark is None:
            return None
        return stock_return - (end_benchmark / start_benchmark - 1) * 100

    lookback = ordered[-60:] if len(ordered) >= 60 else ordered
    last_close = safe_float(ordered[-1].get("close"))
    high_values = [safe_float(row.get("high")) for row in lookback]
    high_60 = max(float(value) for value in high_values if value is not None) if any(value is not None for value in high_values) else None
    distance = (last_close / high_60 - 1) * 100 if last_close and high_60 else None
    return {
        "pre_20d_excess_pct": period_excess(20),
        "pre_60d_excess_pct": period_excess(60),
        "distance_to_60d_high_pct": distance,
    }


def pricing_state(event: Mapping[str, Any], thresholds: GapThresholds) -> str:
    pre20 = safe_float(event.get("pre_20d_excess_pct"))
    pre60 = safe_float(event.get("pre_60d_excess_pct"))
    if pre20 is None:
        return "unknown"
    if pre20 <= thresholds.unpriced_20d_excess_pct and (pre60 is None or pre60 <= thresholds.priced_60d_excess_pct):
        return "unpriced"
    if pre20 >= thresholds.priced_20d_excess_pct or (pre60 is not None and pre60 >= thresholds.priced_60d_excess_pct):
        return "priced"
    return "partial"


def evidence_state(event: Mapping[str, Any], thresholds: GapThresholds) -> str:
    profit_growth = safe_float(event.get("profit_growth_pct"))
    revenue_growth = safe_float(event.get("revenue_growth_pct"))
    acceleration = safe_float(event.get("growth_acceleration_pct"))
    negative = bool(event.get("negative_evidence"))
    evidence_grade = str(event.get("evidence_grade") or "")
    if negative or (profit_growth is not None and profit_growth < -10):
        return "negative"
    if profit_growth is None:
        return "insufficient"
    acceleration_pass = (
        acceleration is not None and acceleration >= thresholds.evidence_acceleration_pct
    ) or (
        acceleration is None and profit_growth >= thresholds.evidence_growth_pct + 20
    )
    revenue_pass = revenue_growth is None or revenue_growth >= -5
    if profit_growth >= thresholds.evidence_growth_pct and acceleration_pass and revenue_pass:
        return "strong"
    if profit_growth >= 0 and evidence_grade in {"A", "B"}:
        return "positive_unconfirmed"
    return "weak"


def classify_event(event: Mapping[str, Any], thresholds: GapThresholds) -> dict[str, str]:
    evidence = evidence_state(event, thresholds)
    pricing = pricing_state(event, thresholds)
    confirmation = safe_float(event.get("market_confirmation_excess_pct"))
    confirmed = confirmation is not None and confirmation >= thresholds.confirmation_excess_pct
    if evidence == "strong" and pricing == "unpriced" and confirmed:
        code = "A"
        reason = "硬证据强、披露前尚未定价，且首个可交易日出现正向市场确认"
    elif evidence == "strong":
        code = "B"
        reason = "硬证据强，但价格已提前交易或首个可交易日尚未确认"
    elif evidence in {"positive_unconfirmed", "insufficient"}:
        code = "C"
        reason = "方向偏正，但硬证据、加速度或定价数据仍需补齐"
    else:
        code = "D"
        reason = "证据为负、偏弱或不满足可验证变化门槛"
    return {"decision_code": code, "evidence_state": evidence, "pricing_state": pricing, "decision_reason": reason}


def _event_excess(event: Mapping[str, Any], horizon: int = 20) -> float | None:
    horizon_data = (event.get("horizons") or {}).get(str(horizon)) or {}
    peer_value = safe_float(horizon_data.get("peer_excess_return_pct"))
    if peer_value is not None:
        return peer_value
    return safe_float(horizon_data.get("market_excess_return_pct"))


def _metric(events: Sequence[Mapping[str, Any]], horizon: int = 20) -> dict[str, Any]:
    mature = [event for event in events if _event_excess(event, horizon) is not None]
    excess = [float(_event_excess(event, horizon)) for event in mature]
    returns = [
        float((event.get("horizons") or {}).get(str(horizon), {}).get("return_pct"))
        for event in mature
        if safe_float((event.get("horizons") or {}).get(str(horizon), {}).get("return_pct")) is not None
    ]
    wins = [value for value in excess if value > 0]
    losses = [value for value in excess if value < 0]
    avg_win = mean(wins)
    avg_loss = abs(mean(losses) or 0) if losses else None
    payoff = avg_win / avg_loss if avg_win is not None and avg_loss else None
    return {
        "sample_count": len(excess),
        "unique_symbols": len({str(event.get("symbol")) for event in mature}),
        "unique_signal_dates": len({str(event.get("signal_date")) for event in mature}),
        "win_rate_pct": round(len(wins) / len(excess) * 100, 4) if excess else None,
        "avg_return_pct": round(mean(returns), 6) if returns else None,
        "avg_excess_return_pct": round(mean(excess), 6) if excess else None,
        "median_excess_return_pct": round(median(excess), 6) if excess else None,
        "p25_excess_return_pct": round(percentile(excess, 0.25), 6) if excess else None,
        "p75_excess_return_pct": round(percentile(excess, 0.75), 6) if excess else None,
        "payoff_ratio": round(payoff, 6) if payoff is not None else None,
    }


def _threshold_grid(market: str) -> list[GapThresholds]:
    growth_values = (20.0, 30.0, 40.0, 50.0) if market == "A股" else (10.0, 20.0, 30.0, 40.0)
    result: list[GapThresholds] = []
    for growth in growth_values:
        for acceleration in (-10.0, 0.0, 10.0, 20.0):
            for unpriced in (-5.0, 0.0, 3.0, 5.0):
                for priced in (10.0, 15.0, 20.0):
                    for confirmation in (-1.0, 0.0, 1.0, 2.0):
                        result.append(GapThresholds(growth, acceleration, unpriced, priced, 25.0, confirmation))
    return result


def select_thresholds(
    events: Sequence[Mapping[str, Any]],
    market: str,
    minimum_a_samples: int = 30,
    minimum_b_samples: int = 20,
) -> tuple[GapThresholds, dict[str, Any]]:
    """Choose thresholds on development data only; callers must freeze them for validation."""

    best: tuple[float, GapThresholds, dict[str, Any]] | None = None
    for thresholds in _threshold_grid(market):
        classified = []
        for event in events:
            item = dict(event)
            item.update(classify_event(item, thresholds))
            classified.append(item)
        a_metric = _metric([event for event in classified if event["decision_code"] == "A"])
        b_metric = _metric([event for event in classified if event["decision_code"] == "B"])
        if a_metric["sample_count"] < minimum_a_samples or b_metric["sample_count"] < minimum_b_samples:
            continue
        a_median = safe_float(a_metric.get("median_excess_return_pct")) or -999
        b_median = safe_float(b_metric.get("median_excess_return_pct")) or 0
        a_win = safe_float(a_metric.get("win_rate_pct")) or 0
        # Development objective rewards absolute edge, A-vs-B pricing gain, and hit rate.
        score = a_median + 0.5 * (a_median - b_median) + 0.02 * (a_win - 50)
        details = {"score": score, "A": a_metric, "B": b_metric}
        if best is None or score > best[0]:
            best = (score, thresholds, details)
    if best is None:
        fallback = DEFAULT_THRESHOLDS[market]
        return fallback, {"status": "fallback", "reason": "development_sample_too_small", "score": None}
    return best[1], {"status": "selected_on_development_only", **best[2]}


def cluster_bootstrap_ci(
    events: Sequence[Mapping[str, Any]],
    horizon: int = 20,
    iterations: int = 1000,
    seed: int = 20260719,
) -> tuple[float | None, float | None]:
    groups: dict[str, list[float]] = defaultdict(list)
    for event in events:
        value = _event_excess(event, horizon)
        if value is not None:
            groups[str(event.get("symbol") or event.get("event_key"))].append(value)
    keys = sorted(groups)
    if len(keys) < 2:
        return None, None
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        chosen = [keys[rng.randrange(len(keys))] for _ in keys]
        values = [value for key in chosen for value in groups[key]]
        # The primary result is the median excess return, so its uncertainty
        # must be bootstrapped on the same robust statistic. Using the mean here
        # would let a split-adjustment anomaly or micro-cap jump decide the gate.
        samples.append(median(values) or 0.0)
    return percentile(samples, 0.025), percentile(samples, 0.975)


def summarize_events(events: Sequence[Mapping[str, Any]], horizon: int = 20) -> dict[str, Any]:
    by_code: dict[str, Any] = {}
    for code in DECISION_CODES:
        subset = [event for event in events if event.get("decision_code") == code]
        metric = _metric(subset, horizon)
        lower, upper = cluster_bootstrap_ci(subset, horizon)
        metric["bootstrap_95ci_low_pct"] = round(lower, 6) if lower is not None else None
        metric["bootstrap_95ci_high_pct"] = round(upper, 6) if upper is not None else None
        by_code[code] = metric

    a_median = safe_float(by_code["A"].get("median_excess_return_pct"))
    b_median = safe_float(by_code["B"].get("median_excess_return_pct"))
    c_median = safe_float(by_code["C"].get("median_excess_return_pct"))
    filter_baseline = median(value for value in (b_median, c_median) if value is not None)
    by_year: dict[str, Any] = {}
    years = sorted({str(event.get("signal_date"))[:4] for event in events if event.get("signal_date")})
    for year in years:
        year_a = [event for event in events if str(event.get("signal_date", "")).startswith(year) and event.get("decision_code") == "A"]
        by_year[year] = _metric(year_a, horizon)
    return {
        "horizon": horizon,
        "by_decision": by_code,
        "pricing_gain_a_vs_b_median_pct": round(a_median - b_median, 6) if a_median is not None and b_median is not None else None,
        "filter_gain_a_vs_bc_median_pct": round(a_median - filter_baseline, 6) if a_median is not None and filter_baseline is not None else None,
        "by_year_A": by_year,
    }


def validation_verdict(summary: Mapping[str, Any], minimum_samples: int = 30) -> dict[str, Any]:
    decisions = summary.get("by_decision") or {}
    a = decisions.get("A") or {}
    sample_count = int(a.get("sample_count") or 0)
    median_excess = safe_float(a.get("median_excess_return_pct"))
    ci_low = safe_float(a.get("bootstrap_95ci_low_pct"))
    pricing_gain = safe_float(summary.get("pricing_gain_a_vs_b_median_pct"))
    yearly = summary.get("by_year_A") or {}
    mature_years = [row for row in yearly.values() if int(row.get("sample_count") or 0) >= 5]
    positive_years = [row for row in mature_years if (safe_float(row.get("median_excess_return_pct")) or 0) > 0]
    checks = {
        "sample_sufficient": sample_count >= minimum_samples,
        "median_positive": median_excess is not None and median_excess > 0,
        "pricing_gain_positive": pricing_gain is not None and pricing_gain > 0,
        "year_consistency": len(mature_years) >= 2 and len(positive_years) / len(mature_years) >= 0.6,
        "bootstrap_lower_positive": ci_low is not None and ci_low > 0,
    }
    strong = all(checks.values())
    useful = checks["sample_sufficient"] and checks["median_positive"] and checks["pricing_gain_positive"] and checks["year_consistency"]
    return {
        "state": "validated" if strong else "useful_with_limits" if useful else "not_validated",
        "checks": checks,
        "interpretation": (
            "留出样本的A类在收益、定价增益、年度一致性和聚类置信区间上均通过。"
            if strong
            else "方向上有用，但统计置信度或年度一致性仍不足，不能把规则写成已稳定验证。"
            if useful
            else "留出样本没有证明该规则稳定有效，应继续保留影子样本并调整证据定义，而不是提高仓位。"
        ),
    }
