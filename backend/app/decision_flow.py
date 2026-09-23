from __future__ import annotations

import math
import os
import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_data_dir, get_database_path, get_db
from app.industry_trends import get_industry_intelligence


router = APIRouter(prefix="/api/investment/decision-flow", tags=["decision-flow"])

DEFAULT_THS_CACHE_PATH = Path(
    "/home/example/Documents/十倍起点/research/backtest_system/ths_group_3y_cache.sqlite"
)
MARKET_CACHE_SECONDS = 15 * 60

STYLE_RULES: dict[str, dict[str, str]] = {
    "进攻扩散": {
        "risk_budget": "0.7–1.0x",
        "action": "允许主线进攻，优先选择硬信息与盘面共振的A类方向。",
    },
    "进攻抱团": {
        "risk_budget": "0.4–0.7x",
        "action": "只做核心表达，不把龙头上涨误判为全板块扩散。",
    },
    "中性轮动": {
        "risk_budget": "0.3–0.5x",
        "action": "缩短验证周期，只在触发后参与，不追逐轮动末端。",
    },
    "防守占优": {
        "risk_budget": "0–0.3x",
        "action": "总风险保持低位；只执行A类低风险表达，B/C类继续等待。",
    },
    "数据形成中": {
        "risk_budget": "0–0.2x",
        "action": "数据不足时不主动放大风险，先补齐行情和成分覆盖。",
    },
}

DISCOVERY_GATE_LABELS = {
    "relative_strength": "相对强度",
    "turnover": "成交确认",
    "persistence": "持续性",
}
CONFIRMATION_GATE_LABELS = {
    "breadth": "板块广度",
    "diffusion": "龙头扩散",
}

INDUSTRY_GROUP_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("光通信", "光互联", "光模块"), ("通信设备", "光学光电子")),
    (("pcb", "印制电路板", "高速交换"), ("元件", "通信设备")),
    (("云计算", "算力租赁", "ai云"), ("通信服务", "it服务", "软件开发")),
    (("国产算力", "超节点", "算力网络"), ("半导体", "计算机设备", "通信设备")),
)

_market_cache: dict[str, Any] = {"loaded_at": 0.0, "source_key": None, "payload": None}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _ratio_text(value: Any) -> str:
    return f"{_finite(value) * 100:.0f}%"


def _latest_date(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(f"select max(trade_date) from {table}").fetchone()
    return str(row[0]) if row and row[0] else None


def _source_key(app_db: Path, ths_db: Path) -> tuple[str | None, str | None]:
    with sqlite3.connect(f"file:{app_db}?mode=ro", uri=True) as connection:
        stock_date = _latest_date(connection, "stock_daily_bars")
    with sqlite3.connect(f"file:{ths_db}?mode=ro", uri=True) as connection:
        ths_date = connection.execute(
            "select max(trade_date) from ths_group_bars where group_type='industry'"
        ).fetchone()[0]
    return stock_date, str(ths_date) if ths_date else None


def _persisted_cache_path() -> Path:
    return get_data_dir() / "decision-flow-market-snapshot.json"


def _load_persisted_market_cache(source_key: tuple[str | None, str | None]) -> dict[str, Any] | None:
    path = _persisted_cache_path()
    if not path.exists():
        return None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if tuple(stored.get("source_key") or ()) != source_key:
        return None
    payload = stored.get("payload")
    return payload if isinstance(payload, dict) else None


def _save_persisted_market_cache(
    source_key: tuple[str | None, str | None],
    payload: dict[str, Any],
) -> None:
    path = _persisted_cache_path()
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(
                {"source_key": list(source_key), "payload": payload},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)


def _load_market_frames(app_db: Path, ths_db: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    with sqlite3.connect(f"file:{ths_db}?mode=ro", uri=True) as connection:
        latest_text = connection.execute(
            "select max(trade_date) from ths_group_bars where group_type='industry'"
        ).fetchone()[0]
        if not latest_text:
            raise ValueError("行业指数缓存为空")
        start_text = (date.fromisoformat(str(latest_text)[:10]) - timedelta(days=190)).isoformat()
        industry = pd.read_sql_query(
            """
            select group_code, group_name, trade_date, open, high, low, close, amount
            from ths_group_bars
            where group_type='industry' and trade_date>=?
            order by group_code, trade_date
            """,
            connection,
            params=[start_text],
            parse_dates=["trade_date"],
        )

    with sqlite3.connect(f"file:{app_db}?mode=ro", uri=True) as connection:
        member = pd.read_sql_query(
            """
            select m.group_code, m.group_name, b.full_code, b.trade_date, b.close
            from market_style_ths_members m
            join stock_daily_bars b on b.full_code=m.full_code
            where m.group_type='industry' and b.trade_date>=?
            order by b.full_code, b.trade_date
            """,
            connection,
            params=[start_text],
            parse_dates=["trade_date"],
        )
    return industry, member


def _add_market_features(industry: pd.DataFrame, member: pd.DataFrame) -> pd.DataFrame:
    if industry.empty or member.empty:
        raise ValueError("市场风格数据不足")

    frame = industry.sort_values(["group_code", "trade_date"]).copy()
    numeric_columns = ["open", "high", "low", "close", "amount"]
    frame[numeric_columns] = frame[numeric_columns].apply(pd.to_numeric, errors="coerce")
    grouped = frame.groupby("group_code", observed=True, group_keys=False)
    frame["return_5d"] = grouped["close"].pct_change(5, fill_method=None)
    frame["return_20d"] = grouped["close"].pct_change(20, fill_method=None)
    frame["ma20"] = grouped["close"].transform(lambda values: values.rolling(20, min_periods=20).mean())
    frame["amount_5d"] = grouped["amount"].transform(lambda values: values.rolling(5, min_periods=5).mean())
    frame["amount_20d"] = grouped["amount"].transform(lambda values: values.rolling(20, min_periods=20).mean())
    frame["amount_ratio_5d_20d"] = frame["amount_5d"] / frame["amount_20d"].replace(0, pd.NA)
    for column in ("return_5d", "return_20d"):
        market_median = frame.groupby("trade_date", observed=True)[column].transform("median")
        frame[f"excess_{column}"] = frame[column] - market_median
        frame[f"{column}_rank"] = frame.groupby("trade_date", observed=True)[column].rank(
            pct=True, method="average"
        )
    frame["top_quartile_return_20d"] = (frame["return_20d_rank"] >= 0.75).astype(float)
    frame["top_quartile_days_last_10"] = frame.groupby("group_code", observed=True)[
        "top_quartile_return_20d"
    ].transform(lambda values: values.rolling(10, min_periods=10).sum())

    close_pivot = frame.pivot(index="trade_date", columns="group_code", values="close").sort_index()
    returns = close_pivot.pct_change(fill_method=None)
    market_return = returns.mean(axis=1, skipna=True)
    market_variance = market_return.rolling(60, min_periods=60).var()
    beta = pd.DataFrame(index=returns.index, columns=returns.columns, dtype=float)
    for column in returns.columns:
        beta[column] = returns[column].rolling(60, min_periods=60).cov(market_return) / market_variance
    beta_long = beta.stack(future_stack=True).rename("beta_60d").reset_index()
    frame = frame.merge(beta_long, on=["trade_date", "group_code"], how="left", validate="one_to_one")
    frame["beta_rank"] = frame.groupby("trade_date", observed=True)["beta_60d"].rank(
        pct=True, method="average"
    )

    members = member.sort_values(["full_code", "trade_date"]).copy()
    members["close"] = pd.to_numeric(members["close"], errors="coerce")
    member_grouped = members.groupby("full_code", observed=True, group_keys=False)
    members["ma20"] = member_grouped["close"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    members["close_20d"] = member_grouped["close"].shift(20)
    members["distance_ma20"] = members["close"] / members["ma20"] - 1
    members["return_20d"] = members["close"] / members["close_20d"] - 1
    members = members.dropna(subset=["distance_ma20", "return_20d"])
    members["above_ma20"] = (members["distance_ma20"] > 0).astype(float)
    members["positive_20d"] = (members["return_20d"] > 0).astype(float)
    members["extreme_20d"] = (members["return_20d"] >= 0.20).astype(float)
    breadth = (
        members.groupby(["group_code", "trade_date"], observed=True)
        .agg(
            observed_members=("full_code", "nunique"),
            breadth_above_ma20=("above_ma20", "mean"),
            breadth_positive_20d=("positive_20d", "mean"),
            extreme_winner_share_20d=("extreme_20d", "mean"),
        )
        .reset_index()
    )
    return frame.merge(
        breadth,
        on=["group_code", "trade_date"],
        how="left",
        validate="one_to_one",
    )


def _rank_discovery_candidates(latest: pd.DataFrame) -> pd.DataFrame:
    """Rank the discovery pool without confirmation or market-style inputs."""
    return latest.sort_values(
        [
            "discovery_qualified",
            "discovery_gate_count",
            "top_quartile_days_last_10",
            "return_20d_rank",
            "amount_ratio_5d_20d",
        ],
        ascending=[False, False, False, False, False],
        kind="mergesort",
    ).head(5)


def _market_snapshot_from_features(frame: pd.DataFrame) -> dict[str, Any]:
    latest_date = frame["trade_date"].max()
    latest = frame.loc[frame["trade_date"].eq(latest_date)].copy()
    latest = latest.dropna(subset=["return_20d", "beta_rank"])
    if latest.empty:
        raise ValueError("市场风格特征尚未形成")

    high_beta = latest.loc[latest["beta_rank"] >= 0.75, "return_20d"].mean()
    low_beta = latest.loc[latest["beta_rank"] <= 0.25, "return_20d"].mean()
    high_low_spread = _finite(high_beta - low_beta)
    market_breadth = _finite(latest["breadth_above_ma20"].mean())
    industry_breadth = _finite((latest["close"] > latest["ma20"]).mean())
    if high_low_spread > 0.02 and market_breadth >= 0.55:
        style_state = "进攻扩散"
    elif high_low_spread > 0.02:
        style_state = "进攻抱团"
    elif high_low_spread < -0.02:
        style_state = "防守占优"
    else:
        style_state = "中性轮动"

    latest["gate_relative_strength"] = (
        (latest["excess_return_5d"] > 0) & (latest["excess_return_20d"] > 0)
    )
    latest["gate_breadth"] = latest["breadth_above_ma20"] >= 0.55
    latest["gate_turnover"] = latest["amount_ratio_5d_20d"] >= 1.05
    latest["gate_diffusion"] = (
        (latest["breadth_positive_20d"] >= 0.50)
        & (latest["extreme_winner_share_20d"] <= 0.30)
    )
    latest["gate_persistence"] = latest["top_quartile_days_last_10"] >= 5
    discovery_gate_columns = [
        "gate_relative_strength",
        "gate_turnover",
        "gate_persistence",
    ]
    confirmation_gate_columns = ["gate_breadth", "gate_diffusion"]
    latest["discovery_gate_count"] = latest[discovery_gate_columns].fillna(False).sum(axis=1)
    latest["discovery_qualified"] = latest[discovery_gate_columns].fillna(False).all(axis=1)
    latest["confirmation_gate_count"] = latest[confirmation_gate_columns].fillna(False).sum(axis=1)
    latest["market_confirmed"] = latest[confirmation_gate_columns].fillna(False).all(axis=1)

    ranked = _rank_discovery_candidates(latest)
    candidates: list[dict[str, Any]] = []
    for rank, row in enumerate(ranked.itertuples(index=False), start=1):
        discovery_gates = []
        for key, label in DISCOVERY_GATE_LABELS.items():
            passed = bool(getattr(row, f"gate_{key}"))
            discovery_gates.append(
                {
                    "key": key,
                    "label": label,
                    "layer": "discover",
                    "status": "pass" if passed else "fail",
                }
            )
        confirmation_gates = []
        for key, label in CONFIRMATION_GATE_LABELS.items():
            passed = bool(getattr(row, f"gate_{key}"))
            confirmation_gates.append(
                {
                    "key": key,
                    "label": label,
                    "layer": "confirm",
                    "status": "pass" if passed else "wait",
                }
            )
        candidates.append(
            {
                "rank": rank,
                "group_code": str(row.group_code),
                "name": str(row.group_name),
                "return_5d_pct": round(_finite(row.return_5d) * 100, 2),
                "return_20d_pct": round(_finite(row.return_20d) * 100, 2),
                "breadth_pct": round(_finite(row.breadth_above_ma20) * 100, 1),
                "amount_ratio": round(_finite(row.amount_ratio_5d_20d), 2),
                "persistence_days": int(_finite(row.top_quartile_days_last_10)),
                "discovery_gate_count": int(row.discovery_gate_count),
                "confirmation_gate_count": int(row.confirmation_gate_count),
                "discovery_qualified": bool(row.discovery_qualified),
                "market_confirmed": bool(row.market_confirmed),
                "discovery_gates": discovery_gates,
                "confirmation_gates": confirmation_gates,
            }
        )

    style_rule = STYLE_RULES[style_state]
    return {
        "as_of": str(latest_date.date()),
        "state": style_state,
        "risk_budget": style_rule["risk_budget"],
        "action": style_rule["action"],
        "metrics": {
            "market_member_breadth": round(market_breadth, 4),
            "industry_index_breadth": round(industry_breadth, 4),
            "high_low_beta_spread_20d": round(high_low_spread, 4),
        },
        "discovery_count": len(candidates),
        "discovery_qualified_count": sum(
            int(candidate["discovery_qualified"]) for candidate in candidates
        ),
        "market_confirmed_count": sum(int(candidate["market_confirmed"]) for candidate in candidates),
        "candidates": candidates,
    }


def load_market_snapshot(force: bool = False) -> dict[str, Any]:
    app_db = get_database_path()
    ths_db = Path(os.environ.get("THS_GROUP_CACHE_PATH", str(DEFAULT_THS_CACHE_PATH))).expanduser()
    if not ths_db.exists():
        raise FileNotFoundError(f"行业指数缓存不存在：{ths_db}")
    source_key = _source_key(app_db, ths_db)
    if (
        not force
        and _market_cache["payload"] is not None
        and _market_cache["source_key"] == source_key
        and time.monotonic() - float(_market_cache["loaded_at"]) < MARKET_CACHE_SECONDS
    ):
        return _market_cache["payload"]
    if not force:
        persisted = _load_persisted_market_cache(source_key)
        if persisted is not None:
            _market_cache.update(
                {"loaded_at": time.monotonic(), "source_key": source_key, "payload": persisted}
            )
            return persisted
    industry, member = _load_market_frames(app_db, ths_db)
    payload = _market_snapshot_from_features(_add_market_features(industry, member))
    _save_persisted_market_cache(source_key, payload)
    _market_cache.update({"loaded_at": time.monotonic(), "source_key": source_key, "payload": payload})
    return payload


def _industry_matches(candidate_name: str, industry_name: str) -> bool:
    candidate = candidate_name.lower().replace(" ", "")
    industry = industry_name.lower().replace(" ", "")
    if candidate in industry or industry in candidate:
        return True
    for industry_keywords, group_keywords in INDUSTRY_GROUP_HINTS:
        if any(keyword in industry for keyword in industry_keywords):
            return any(keyword in candidate for keyword in group_keywords)
    return False


def _market_candidate_item(candidate: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    missing_discovery = [
        gate["label"] for gate in candidate["discovery_gates"] if gate["status"] != "pass"
    ]
    missing_confirmation = [
        gate["label"] for gate in candidate["confirmation_gates"] if gate["status"] != "pass"
    ]
    discovery_text = (
        "三项发现条件已通过"
        if not missing_discovery
        else f"发现层仍缺：{'、'.join(missing_discovery)}"
    )
    confirmation_text = (
        "广度与扩散已确认"
        if not missing_confirmation
        else f"确认层仍缺：{'、'.join(missing_confirmation)}"
    )
    return {
        "key": f"market:{candidate['group_code']}",
        "decision_code": "C",
        "decision_label": "等待信息确认",
        "name": candidate["name"],
        "source": "市场候选",
        "primary_company": None,
        "cycle": "20个交易日验证",
        "pricing_status": "待判断",
        "current_action": (
            f"进入发现层 Top 5（第 {candidate['rank']} 名）；"
            "广度、扩散和硬信息未确认前不升级。"
        ),
        "trigger": "广度与扩散确认后，再补官方或公司硬事实及利润传导。",
        "invalidation": "相对强度、成交或持续性失守时退出 Top 5；确认层转弱只降级观察。",
        "link": f"/investment/information-screening?keyword={candidate['name']}",
        "link_label": "去核信息",
        "gates": [
            *candidate["discovery_gates"],
            *candidate["confirmation_gates"],
            {"key": "information", "label": "硬信息", "layer": "confirm", "status": "wait"},
        ],
        "facts": [
            f"发现层第 {candidate['rank']} 名 · 市场风格仅控制仓位（{market['state']}）",
            f"5日 {candidate['return_5d_pct']:+.2f}% · 20日 {candidate['return_20d_pct']:+.2f}%",
            f"站上20日线 {candidate['breadth_pct']:.0f}% · 量能 {candidate['amount_ratio']:.2f}x",
            f"{discovery_text} · {confirmation_text}",
        ],
    }


def _industry_item(
    sector: dict[str, Any],
    market: dict[str, Any],
    market_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence = sector.get("evidence_summary") or {}
    expression = sector.get("expression") or {}
    hard_confirmed = int(evidence.get("hard") or 0) > 0 and sector.get("evidence_status") == "硬变化已确认"
    direct_transmission = sector.get("direction_verdict") == "通过" and bool(expression.get("verified"))
    pricing_ok = sector.get("pricing_status") != "充分定价"
    direction_failed = sector.get("direction_verdict") == "否决" or int(evidence.get("risk") or 0) > 0
    matched_candidate = next(
        (
            candidate
            for candidate in market_candidates
            if _industry_matches(candidate["name"], str(sector.get("name") or ""))
        ),
        None,
    )
    discovered = bool(
        matched_candidate and matched_candidate.get("discovery_qualified")
    )
    market_confirmed = bool(
        expression.get("market_confirmed")
        or (matched_candidate and matched_candidate.get("market_confirmed"))
    )

    if direction_failed:
        code, label = "D", "剔除"
    elif (
        hard_confirmed
        and direct_transmission
        and discovered
        and market_confirmed
        and pricing_ok
        and sector.get("action_tone") == "positive"
    ):
        code, label = "A", "进入交易计划"
    elif hard_confirmed and direct_transmission:
        code, label = "B", "等待价格或触发"
    else:
        code, label = "C", "等待信息确认"

    trigger = str(sector.get("next_signal") or "").strip()
    catalyst = sector.get("next_catalyst") or {}
    if catalyst.get("event_name"):
        catalyst_text = str(catalyst["event_name"])
        if catalyst.get("expected_time"):
            catalyst_text += f"（{catalyst['expected_time']}）"
        trigger = f"{catalyst_text}；{trigger}" if trigger else catalyst_text

    if matched_candidate:
        discovery_gates = list(matched_candidate["discovery_gates"])
        confirmation_gates = list(matched_candidate["confirmation_gates"])
    else:
        discovery_gates = [
            {
                "key": "discovery",
                "label": "Top 5 发现",
                "layer": "discover",
                "status": "wait",
            }
        ]
        confirmation_gates = [
            {
                "key": "market_confirmation",
                "label": "广度 / 扩散",
                "layer": "confirm",
                "status": "pass" if market_confirmed else "wait",
            }
        ]

    return {
        "key": f"industry:{sector.get('id')}",
        "decision_code": code,
        "decision_label": label,
        "name": sector.get("name"),
        "source": "产业研究",
        "primary_company": sector.get("primary_company"),
        "cycle": sector.get("phase") or "待确认",
        "pricing_status": sector.get("pricing_status") or "待判断",
        "current_action": sector.get("action_reason") or sector.get("action") or "等待下一步确认。",
        "trigger": trigger or "补充下一条可观察、可证伪的触发条件。",
        "invalidation": sector.get("invalidation") or "补充明确证伪条件后再升级。",
        "link": f"/investment/industry-trend/research?trend={sector.get('id')}",
        "link_label": "查看研究",
        "gates": [
            *discovery_gates,
            *confirmation_gates,
            {
                "key": "information",
                "label": "硬信息",
                "layer": "confirm",
                "status": "pass" if hard_confirmed else "wait",
            },
            {
                "key": "transmission",
                "label": "直接传导",
                "layer": "confirm",
                "status": "pass" if direct_transmission else "wait",
            },
            {
                "key": "pricing",
                "label": "定价空间",
                "layer": "confirm",
                "status": "pass" if pricing_ok else "fail",
            },
        ],
        "facts": [
            (
                f"发现层：Top 5 第 {matched_candidate['rank']} 名"
                if matched_candidate
                else "发现层：当前未进入 Top 5"
            ),
            f"硬证据 {int(evidence.get('hard') or 0)} 条 · 互证 {int(evidence.get('mutual') or 0)} 条",
            f"首选表达：{sector.get('primary_company') or '待选择'}",
            f"市场风格：{market['state']}（只用于风险预算）",
        ],
    }


def build_decision_flow_payload(
    market: dict[str, Any],
    intelligence: dict[str, Any],
    today: date | None = None,
) -> dict[str, Any]:
    current_date = today or date.today()
    market_date = date.fromisoformat(str(market["as_of"])[:10])
    lag_days = max((current_date - market_date).days, 0)
    market_candidates = list(market.get("candidates") or [])
    industry_items = [
        _industry_item(sector, market, market_candidates)
        for sector in intelligence.get("sectors") or []
    ]
    matched_market_names = {
        candidate["name"]
        for candidate in market_candidates
        if any(_industry_matches(candidate["name"], str(sector.get("name") or "")) for sector in intelligence.get("sectors") or [])
    }
    market_items = [
        _market_candidate_item(candidate, market)
        for candidate in market_candidates
        if candidate["name"] not in matched_market_names
    ]
    items = industry_items + market_items
    code_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    items.sort(key=lambda item: (code_order[item["decision_code"]], item["source"] != "产业研究", item["name"]))
    queues = {
        code: [item for item in items if item["decision_code"] == code]
        for code in ("A", "B", "C", "D")
    }
    counts = {code: len(rows) for code, rows in queues.items()}
    primary = queues["A"][0] if queues["A"] else queues["B"][0] if queues["B"] else None
    headline_action = (
        f"执行 {primary['name']} 的{primary['decision_code']}类计划；其余方向不抢跑。"
        if primary
        else "当前没有可执行方向，保持观察。"
    )
    if market["state"] == "防守占优":
        headline_action = f"{headline_action} 总风险控制在 {market['risk_budget']}。"

    hard_information_count = sum(
        any(
            gate["key"] == "information" and gate["status"] == "pass"
            for gate in item["gates"]
        )
        for item in industry_items
    )
    steps = [
        {
            "key": "discover",
            "number": "01",
            "label": "发现层",
            "status": f"{market.get('discovery_count', 0)} 个",
            "detail": "强势 + 量能 + 持续性，只生成 Top 5",
        },
        {
            "key": "confirm",
            "number": "02",
            "label": "确认层",
            "status": f"{market.get('market_confirmed_count', 0)} 个盘面确认",
            "detail": f"广度 + 扩散；{hard_information_count} 个方向已有硬信息",
        },
        {
            "key": "position",
            "number": "03",
            "label": "仓位层",
            "status": market["state"],
            "detail": f"只决定风险预算 {market['risk_budget']}，不改变行业排序",
        },
    ]
    return {
        "status": "ok",
        "as_of": market["as_of"],
        "data_status": "stale" if lag_days > 3 else "current",
        "lag_days": lag_days,
        "headline": {
            "market_style": market["state"],
            "risk_budget": market["risk_budget"],
            "action": headline_action,
            "style_action": market["action"],
        },
        "market_metrics": market.get("metrics") or {},
        "steps": steps,
        "counts": counts,
        "queues": queues,
        "rule": "强势、量能、持续性负责发现；广度、扩散和硬信息负责确认；市场风格只控制仓位。",
        "decision_definitions": {
            "A": "进入 Top 5，广度/扩散确认，硬变化、直接传导和定价空间同时成立。",
            "B": "硬逻辑成立，但尚未进入 Top 5，或价格与催化暂不合适。",
            "C": "进入发现层，但广度、扩散或硬信息仍不足。",
            "D": "方向证伪、传导不成立或已无赔率。",
        },
    }


@router.get("")
def get_decision_flow(
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        market = load_market_snapshot(force=refresh)
    except Exception as exc:  # noqa: BLE001
        market = {
            "as_of": date.today().isoformat(),
            "state": "数据形成中",
            "risk_budget": STYLE_RULES["数据形成中"]["risk_budget"],
            "action": STYLE_RULES["数据形成中"]["action"],
            "metrics": {},
            "discovery_count": 0,
            "discovery_qualified_count": 0,
            "market_confirmed_count": 0,
            "candidates": [],
            "error": str(exc)[:240],
        }
    intelligence = get_industry_intelligence(db)
    payload = build_decision_flow_payload(market, intelligence)
    if market.get("error"):
        payload["status"] = "partial"
        payload["message"] = market["error"]
    return payload
