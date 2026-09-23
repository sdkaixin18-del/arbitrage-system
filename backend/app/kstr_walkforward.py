from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import fmean, median, pstdev
from typing import Iterable, Literal
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _symmetric_spread_pct(left: float, right: float) -> float:
    return 2 * (left - right) / (left + right) * 100


def _sample_beta(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    x_mean = fmean(row[0] for row in pairs)
    y_mean = fmean(row[1] for row in pairs)
    variance = sum((row[0] - x_mean) ** 2 for row in pairs)
    if variance <= 0:
        return None
    covariance = sum((row[0] - x_mean) * (row[1] - y_mean) for row in pairs)
    return covariance / variance


@dataclass(frozen=True)
class QuoteSnapshot:
    timestamp: datetime
    a_bid: float | None
    a_ask: float | None
    kstr_bid: float | None
    kstr_ask: float | None
    usd_cny: float | None
    a_market_phase: str = "continuous"
    a_quote_fresh: bool = True
    kstr_quote_fresh: bool = True
    dynamic_beta: float | None = None
    dynamic_beta_asof: datetime | None = None

    def normalized(self) -> "QuoteSnapshot":
        return QuoteSnapshot(
            timestamp=_utc(self.timestamp),
            a_bid=self.a_bid,
            a_ask=self.a_ask,
            kstr_bid=self.kstr_bid,
            kstr_ask=self.kstr_ask,
            usd_cny=self.usd_cny,
            a_market_phase=self.a_market_phase,
            a_quote_fresh=self.a_quote_fresh,
            kstr_quote_fresh=self.kstr_quote_fresh,
            dynamic_beta=self.dynamic_beta,
            dynamic_beta_asof=_utc(self.dynamic_beta_asof) if self.dynamic_beta_asof else None,
        )


@dataclass(frozen=True)
class FundingEvent:
    timestamp: datetime
    rate: float

    def normalized(self) -> "FundingEvent":
        if not math.isfinite(self.rate):
            raise ValueError("funding rate must be finite")
        return FundingEvent(timestamp=_utc(self.timestamp), rate=self.rate)


@dataclass(frozen=True)
class ReturnPair:
    timestamp: datetime
    a_return: float
    kstr_cny_return: float

    def normalized(self) -> "ReturnPair":
        if not math.isfinite(self.a_return) or not math.isfinite(self.kstr_cny_return):
            raise ValueError("return pair values must be finite")
        return ReturnPair(
            timestamp=_utc(self.timestamp),
            a_return=self.a_return,
            kstr_cny_return=self.kstr_cny_return,
        )


@dataclass(frozen=True)
class WalkForwardConfig:
    baseline_lookback_calendar_days: int = 32
    min_baseline_trading_days: int = 20
    min_baseline_points: int = 300
    entry_spread_pct: float = 2.0
    entry_z: float = 1.5
    exit_spread_pct: float = 0.0
    max_holding_trading_days: int = 5
    enforce_t_plus_one: bool = True
    beta_mode: Literal["fixed", "dynamic"] = "fixed"
    fixed_beta: float = 1.0
    kstr_quantity: float = 100.0
    a_lot_size: int = 100
    kstr_fee_bps_per_side: float = 0.0
    a_fee_bps_per_side: float = 0.0
    slippage_bps_per_leg: float = 0.0
    costs_verified: bool = False
    minimum_required_trading_days: int = 60
    minimum_required_signal_clusters: int = 30

    def validate(self) -> None:
        if self.baseline_lookback_calendar_days <= 0:
            raise ValueError("baseline lookback must be positive")
        if self.min_baseline_trading_days <= 0 or self.min_baseline_points <= 1:
            raise ValueError("baseline minimums must be positive")
        if self.max_holding_trading_days < 1:
            raise ValueError("max holding days must be at least one")
        if self.kstr_quantity <= 0 or self.a_lot_size <= 0:
            raise ValueError("position sizes must be positive")
        if self.beta_mode not in {"fixed", "dynamic"}:
            raise ValueError("unsupported beta mode")


@dataclass(frozen=True)
class PointInTimeFeature:
    quote: QuoteSnapshot
    executable: bool
    reference_ratio: float | None
    baseline_points: int
    baseline_trading_days: int
    history_mean_pct: float | None
    history_std_pct: float | None
    mid_spread_pct: float | None
    open_spread_pct: float | None
    close_spread_pct: float | None
    open_z: float | None
    signal: bool
    beta: float | None
    blocked_reason: str | None


@dataclass(frozen=True)
class Trade:
    entry_time: datetime
    exit_time: datetime
    entry_spread_pct: float
    exit_spread_pct: float
    entry_z: float
    beta: float
    kstr_quantity: float
    a_quantity: int
    holding_trading_days: int
    holding_calendar_hours: float
    gross_pnl_cny: float
    funding_pnl_cny: float
    fees_cny: float
    net_pnl_cny: float
    entry_gross_exposure_cny: float
    net_return_on_gross_pct: float
    exit_reason: str


def quote_is_executable(quote: QuoteSnapshot) -> bool:
    return bool(
        quote.a_market_phase == "continuous"
        and quote.a_quote_fresh
        and quote.kstr_quote_fresh
        and _positive(quote.a_bid)
        and _positive(quote.a_ask)
        and _positive(quote.kstr_bid)
        and _positive(quote.kstr_ask)
        and _positive(quote.usd_cny)
        and quote.a_bid <= quote.a_ask
        and quote.kstr_bid <= quote.kstr_ask
    )


def _quote_mid_ratio(quote: QuoteSnapshot) -> float:
    assert quote.a_bid is not None and quote.a_ask is not None
    assert quote.kstr_bid is not None and quote.kstr_ask is not None
    assert quote.usd_cny is not None
    a_mid = (quote.a_bid + quote.a_ask) / 2
    kstr_mid = (quote.kstr_bid + quote.kstr_ask) / 2
    return kstr_mid * quote.usd_cny / a_mid


def _beta_for_quote(quote: QuoteSnapshot, config: WalkForwardConfig) -> tuple[float | None, str | None]:
    if config.beta_mode == "fixed":
        return config.fixed_beta, None
    if quote.dynamic_beta is None or not math.isfinite(quote.dynamic_beta):
        return None, "missing point-in-time dynamic beta"
    if quote.dynamic_beta_asof is None or quote.dynamic_beta_asof >= quote.timestamp:
        return None, "dynamic beta is not strictly prior to the quote"
    return max(0.6, min(float(quote.dynamic_beta), 1.4)), None


def build_point_in_time_features(
    quotes: Iterable[QuoteSnapshot],
    config: WalkForwardConfig,
) -> list[PointInTimeFeature]:
    config.validate()
    normalized = sorted((quote.normalized() for quote in quotes), key=lambda row: row.timestamp)
    timestamps = [row.timestamp for row in normalized]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate quote timestamps are not allowed")

    executable_history: list[tuple[datetime, date, float]] = []
    features: list[PointInTimeFeature] = []
    for quote in normalized:
        executable = quote_is_executable(quote)
        cutoff = quote.timestamp - timedelta(days=config.baseline_lookback_calendar_days)
        history = [row for row in executable_history if row[0] >= cutoff]
        ratios = [row[2] for row in history]
        trading_days = len({row[1] for row in history})
        reference = median(ratios) if ratios else None
        history_spreads = (
            [_symmetric_spread_pct(value, reference) for value in ratios]
            if reference is not None
            else []
        )
        history_mean = fmean(history_spreads) if history_spreads else None
        history_std = pstdev(history_spreads) if len(history_spreads) >= 2 else None
        mid_spread = None
        open_spread = None
        close_spread = None
        open_z = None
        blocked_reason = None
        beta, beta_reason = _beta_for_quote(quote, config)

        baseline_ready = bool(
            reference is not None
            and len(history) >= config.min_baseline_points
            and trading_days >= config.min_baseline_trading_days
            and history_std is not None
            and history_std > 0
        )
        if not executable:
            blocked_reason = "quotes are not synchronously executable"
        elif not baseline_ready:
            blocked_reason = "point-in-time baseline is incomplete"
        elif beta_reason:
            blocked_reason = beta_reason
        else:
            assert quote.a_bid is not None and quote.a_ask is not None
            assert quote.kstr_bid is not None and quote.kstr_ask is not None
            assert quote.usd_cny is not None and reference is not None
            mid_ratio = _quote_mid_ratio(quote)
            open_ratio = quote.kstr_bid * quote.usd_cny / quote.a_ask
            close_ratio = quote.kstr_ask * quote.usd_cny / quote.a_bid
            mid_spread = _symmetric_spread_pct(mid_ratio, reference)
            open_spread = _symmetric_spread_pct(open_ratio, reference)
            close_spread = _symmetric_spread_pct(close_ratio, reference)
            assert history_mean is not None and history_std is not None
            open_z = (open_spread - history_mean) / history_std

        signal = bool(
            blocked_reason is None
            and open_spread is not None
            and open_spread >= config.entry_spread_pct
            and open_z is not None
            and open_z >= config.entry_z
        )
        features.append(
            PointInTimeFeature(
                quote=quote,
                executable=executable,
                reference_ratio=reference,
                baseline_points=len(history),
                baseline_trading_days=trading_days,
                history_mean_pct=history_mean,
                history_std_pct=history_std,
                mid_spread_pct=mid_spread,
                open_spread_pct=open_spread,
                close_spread_pct=close_spread,
                open_z=open_z,
                signal=signal,
                beta=beta,
                blocked_reason=blocked_reason,
            )
        )
        if executable:
            executable_history.append(
                (
                    quote.timestamp,
                    quote.timestamp.astimezone(SHANGHAI).date(),
                    _quote_mid_ratio(quote),
                )
            )
    return features


def _round_lot(value: float, lot_size: int) -> int:
    return max(lot_size, int(math.floor(value / lot_size + 0.5)) * lot_size)


def _funding_pnl(
    events: list[FundingEvent],
    entry_time: datetime,
    exit_time: datetime,
    short_notional_cny: float,
) -> tuple[float, int]:
    selected = [row.rate for row in events if entry_time < row.timestamp <= exit_time]
    return short_notional_cny * sum(selected), len(selected)


def _max_drawdown_pct(returns_pct: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns_pct:
        equity *= 1 + value / 100
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1)
    return worst * 100


def run_walk_forward(
    quotes: Iterable[QuoteSnapshot],
    funding_events: Iterable[FundingEvent],
    config: WalkForwardConfig,
) -> dict:
    features = build_point_in_time_features(quotes, config)
    funding = sorted((row.normalized() for row in funding_events), key=lambda row: row.timestamp)
    execution_dates = sorted(
        {
            feature.quote.timestamp.astimezone(SHANGHAI).date()
            for feature in features
            if feature.executable
        }
    )
    date_ordinal = {value: index for index, value in enumerate(execution_dates)}

    signal_clusters = 0
    signal_active = False
    for feature in features:
        if feature.signal and not signal_active:
            signal_clusters += 1
        signal_active = feature.signal

    position: dict | None = None
    trades: list[Trade] = []
    for feature in features:
        quote = feature.quote
        local_day = quote.timestamp.astimezone(SHANGHAI).date()
        if position is not None and feature.blocked_reason is None:
            entry_day = position["entry_day"]
            holding_days = date_ordinal.get(local_day, 0) - date_ordinal.get(entry_day, 0)
            t_plus_one_ready = not config.enforce_t_plus_one or local_day > entry_day
            mean_exit = (
                feature.close_spread_pct is not None
                and feature.close_spread_pct <= config.exit_spread_pct
            )
            timed_exit = holding_days >= config.max_holding_trading_days
            if t_plus_one_ready and (mean_exit or timed_exit):
                assert quote.a_bid is not None and quote.kstr_ask is not None and quote.usd_cny is not None
                slip = config.slippage_bps_per_leg / 10_000
                exit_kstr_buy = quote.kstr_ask * (1 + slip)
                exit_a_sell = quote.a_bid * (1 - slip)
                kstr_pnl = config.kstr_quantity * (
                    position["entry_kstr_sell"] * position["entry_fx"]
                    - exit_kstr_buy * quote.usd_cny
                )
                a_pnl = position["a_quantity"] * (exit_a_sell - position["entry_a_buy"])
                funding_pnl, _ = _funding_pnl(
                    funding,
                    position["entry_time"],
                    quote.timestamp,
                    position["entry_kstr_notional_cny"],
                )
                kstr_fee = (
                    position["entry_kstr_notional_cny"]
                    + config.kstr_quantity * exit_kstr_buy * quote.usd_cny
                ) * config.kstr_fee_bps_per_side / 10_000
                a_fee = (
                    position["entry_a_notional_cny"]
                    + position["a_quantity"] * exit_a_sell
                ) * config.a_fee_bps_per_side / 10_000
                fees = kstr_fee + a_fee
                gross_pnl = kstr_pnl + a_pnl
                net_pnl = gross_pnl + funding_pnl - fees
                gross_exposure = (
                    position["entry_kstr_notional_cny"] + position["entry_a_notional_cny"]
                )
                trades.append(
                    Trade(
                        entry_time=position["entry_time"],
                        exit_time=quote.timestamp,
                        entry_spread_pct=position["entry_spread_pct"],
                        exit_spread_pct=float(feature.close_spread_pct),
                        entry_z=position["entry_z"],
                        beta=position["beta"],
                        kstr_quantity=config.kstr_quantity,
                        a_quantity=position["a_quantity"],
                        holding_trading_days=holding_days,
                        holding_calendar_hours=(
                            quote.timestamp - position["entry_time"]
                        ).total_seconds()
                        / 3600,
                        gross_pnl_cny=gross_pnl,
                        funding_pnl_cny=funding_pnl,
                        fees_cny=fees,
                        net_pnl_cny=net_pnl,
                        entry_gross_exposure_cny=gross_exposure,
                        net_return_on_gross_pct=net_pnl / gross_exposure * 100,
                        exit_reason="mean_reversion" if mean_exit else "time_stop",
                    )
                )
                position = None
                continue

        if position is None and feature.signal:
            assert feature.beta is not None and feature.open_spread_pct is not None and feature.open_z is not None
            assert quote.a_ask is not None and quote.kstr_bid is not None and quote.usd_cny is not None
            slip = config.slippage_bps_per_leg / 10_000
            entry_kstr_sell = quote.kstr_bid * (1 - slip)
            entry_a_buy = quote.a_ask * (1 + slip)
            target_a_quantity = (
                config.kstr_quantity
                * entry_kstr_sell
                * quote.usd_cny
                * feature.beta
                / entry_a_buy
            )
            a_quantity = _round_lot(target_a_quantity, config.a_lot_size)
            position = {
                "entry_time": quote.timestamp,
                "entry_day": local_day,
                "entry_spread_pct": feature.open_spread_pct,
                "entry_z": feature.open_z,
                "beta": feature.beta,
                "entry_kstr_sell": entry_kstr_sell,
                "entry_a_buy": entry_a_buy,
                "entry_fx": quote.usd_cny,
                "a_quantity": a_quantity,
                "entry_kstr_notional_cny": config.kstr_quantity * entry_kstr_sell * quote.usd_cny,
                "entry_a_notional_cny": a_quantity * entry_a_buy,
            }

    return_pct = [row.net_return_on_gross_pct for row in trades]
    executable_points = sum(feature.executable for feature in features)
    book_completeness_pct = executable_points / len(features) * 100 if features else 0.0
    metrics = {
        "tradeCount": len(trades),
        "winRatePct": (
            sum(row.net_pnl_cny > 0 for row in trades) / len(trades) * 100 if trades else None
        ),
        "meanNetReturnOnGrossPct": fmean(return_pct) if return_pct else None,
        "medianNetReturnOnGrossPct": median(return_pct) if return_pct else None,
        "totalNetPnlCny": sum(row.net_pnl_cny for row in trades),
        "maxDrawdownPct": _max_drawdown_pct(return_pct) if return_pct else None,
        "meanHoldingTradingDays": (
            fmean(row.holding_trading_days for row in trades) if trades else None
        ),
        "meanFundingPnlCny": fmean(row.funding_pnl_cny for row in trades) if trades else None,
    }
    readiness_reasons: list[str] = []
    if len(execution_dates) < config.minimum_required_trading_days:
        readiness_reasons.append(
            f"executable trading days {len(execution_dates)} < {config.minimum_required_trading_days}"
        )
    if signal_clusters < config.minimum_required_signal_clusters:
        readiness_reasons.append(
            f"independent signal clusters {signal_clusters} < {config.minimum_required_signal_clusters}"
        )
    if len(trades) < config.minimum_required_signal_clusters:
        readiness_reasons.append(
            f"closed trades {len(trades)} < {config.minimum_required_signal_clusters}"
        )
    if book_completeness_pct < 100:
        readiness_reasons.append(
            f"executable bid/ask completeness {book_completeness_pct:.2f}% < 100%"
        )
    if not config.costs_verified:
        readiness_reasons.append(
            "account-specific fees and slippage assumptions are not verified"
        )
    return {
        "status": "validated" if not readiness_reasons else "insufficient_sample",
        "config": asdict(config),
        "dataQuality": {
            "quotePoints": len(features),
            "executableQuotePoints": executable_points,
            "executableBidAskCompletenessPct": book_completeness_pct,
            "executableTradingDays": len(execution_dates),
            "fundingEvents": len(funding),
            "independentSignalClusters": signal_clusters,
        },
        "metrics": metrics,
        "readinessReasons": readiness_reasons,
        "openPositionAtEnd": position is not None,
        "trades": [asdict(row) for row in trades],
    }


def point_in_time_dynamic_beta(
    pairs: Iterable[ReturnPair],
    windows: tuple[int, ...] = (20, 60, 120),
    lower_bound: float = 0.6,
    upper_bound: float = 1.4,
    require_all_windows: bool = True,
) -> list[tuple[datetime, float | None]]:
    normalized = sorted((row.normalized() for row in pairs), key=lambda row: row.timestamp)
    timestamps = [row.timestamp for row in normalized]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate return timestamps are not allowed")
    output: list[tuple[datetime, float | None]] = []
    for position, row in enumerate(normalized):
        estimates: list[float] = []
        for window in windows:
            history = normalized[max(0, position - window) : position]
            if len(history) < window:
                continue
            estimate = _sample_beta(
                [(item.a_return, item.kstr_cny_return) for item in history]
            )
            if estimate is not None and math.isfinite(estimate):
                estimates.append(estimate)
        enough_estimates = (
            len(estimates) == len(windows) if require_all_windows else bool(estimates)
        )
        dynamic = median(estimates) if enough_estimates else None
        if dynamic is not None:
            dynamic = max(lower_bound, min(dynamic, upper_bound))
        output.append((row.timestamp, dynamic))
    return output


def compare_beta_methods(
    pairs: Iterable[ReturnPair],
    fixed_beta: float = 1.0,
    windows: tuple[int, ...] = (20, 60, 120),
) -> dict:
    normalized = sorted((row.normalized() for row in pairs), key=lambda row: row.timestamp)
    dynamic_by_time = dict(point_in_time_dynamic_beta(normalized, windows))
    fixed_residuals: list[float] = []
    dynamic_residuals: list[float] = []
    for row in normalized:
        dynamic = dynamic_by_time[row.timestamp]
        if dynamic is None:
            continue
        fixed_residuals.append(row.kstr_cny_return - fixed_beta * row.a_return)
        dynamic_residuals.append(row.kstr_cny_return - dynamic * row.a_return)

    def summary(values: list[float]) -> dict:
        return {
            "sampleCount": len(values),
            "residualStdPct": pstdev(values) * 100 if len(values) >= 2 else None,
            "residualMaePct": fmean(abs(value) for value in values) * 100 if values else None,
        }

    return {
        "fixed": {"beta": fixed_beta, **summary(fixed_residuals)},
        "dynamic": {"windows": list(windows), **summary(dynamic_residuals)},
    }
