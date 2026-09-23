from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, desc, or_, select
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_data_dir, get_db
from app.cross_market import build_cross_market_intelligence
from app.decision_policy import evaluate_industry_policies
from app.models import (
    FactorMarketCapSnapshot,
    FactorQuoteSnapshot,
    InformationScreeningItem,
    IndustryChain,
    IndustryChainCompany,
    IndustryChainEvidence,
    IndustryChainTask,
    IndustryTrendCatalyst,
    IndustryTrendDraft,
    IndustryTrendEdge,
    IndustryTrendGenerationJob,
    IndustryTrendDecision,
    IndustryTrendMaterial,
    IndustryTrendNode,
    IndustryTrendNodeSource,
    IndustryTrendResearchSetting,
    IndustryTrendUpdate,
    IndustryTrendVersion,
    IndustryExpectationSnapshot,
    StockDailyBar,
    XueqiuPost,
    now_utc,
)
from app.research_runs import ensure_stock


router = APIRouter(prefix="/api/investment/industry-trends", tags=["industry-trends"])

PHASES = ["观察期", "萌芽期", "验证期", "增长期", "爆发期", "成熟期", "退潮期"]
ATTENTION_LEVELS = ["重点跟踪", "持续跟踪", "观察", "暂停"]
VERDICTS = ["通过", "观察", "否决"]
PRICING_STATUSES = ["未定价", "部分定价", "充分定价"]
EXPECTATION_GAP_STATUSES = ["正向预期差", "基本匹配", "负向预期差", "无法判断"]
NODE_TYPES = ["需求驱动", "网络与系统", "光互联产品", "核心器件", "制造与配套"]
NODE_TYPE_ALIASES = {
    "需求端": "需求驱动",
    "核心环节": "光互联产品",
    "价值量集中环节": "核心器件",
    "A股公司": "制造与配套",
    "全球公司": "制造与配套",
}
MATURITY_STATUSES = ["前沿储备", "验证中", "小批量", "放量中", "成熟应用"]
MARKETS = ["A股", "美股", "台湾", "韩国", "日本", "其他"]
MARKET_PREFIXES = {"美股": "US", "台湾": "TW", "韩国": "KR", "日本": "JP", "其他": "GLOBAL"}
MARKET_DEFAULT_EXCHANGES = {"美股": "US", "台湾": "TW", "韩国": "KR", "日本": "JP", "其他": "GLOBAL"}
LEVELS = ["强", "中", "弱"]
SOURCE_TIERS = ["官方硬证据", "公司披露", "行业标准", "市场数据", "雪球线索", "Codex判断"]
SOURCE_VERIFICATION_STATUSES = ["已互证", "单一来源", "市场线索", "待验证"]
EVIDENCE_STATES = ["有效", "待复核", "已失效", "存在冲突"]
MATERIAL_CHANGE_TYPES = ["新增证据", "信息冲突", "证据失效", "待验证"]
MATERIAL_STATUSES = ["待处理", "已纳入", "忽略"]
TRACKING_STATUSES = ["核心受益", "重点跟踪", "观察", "淘汰"]
VERIFICATION_STATUSES = ["未验证", "验证中", "已确认", "失败"]
CATALYST_STATUSES = ["预期", "确认", "兑现"]
CATALYST_IMPORTANCE_LEVELS = ["高", "中", "低"]
CAUSAL_STAGES = ["真实变化", "认知扩散", "资金进入", "筹码交换", "拥挤退潮"]
CAUSAL_EVIDENCE_TYPES = ["硬事实", "市场线索", "盘面确认", "市场推断"]
CAUSAL_SIGNAL_STATUSES = ["线索", "已确认", "减弱", "失效"]
SELL_PRESSURE_LEVELS = ["低", "中", "高"]
JOB_STATUSES_ACTIVE = {"queued", "running"}
DEFAULT_SOURCE_PREFERENCES = ["公告/财报", "公司官网", "政府/监管", "行业组织", "市场行情", "雪球线索"]
DEFAULT_EVIDENCE_RULES = "正式结论优先使用一手来源；核心判断至少由两个独立来源互证。雪球、同花顺和媒体只作为线索，不能直接覆盖正式判断。"

CHAIN_FIELDS = (
    "name",
    "summary",
    "phase",
    "strength",
    "catalyst",
    "risk",
    "investment_logic",
    "change_summary",
    "why_now",
    "expected_duration",
    "attention_level",
    "direction_verdict",
    "stock_verdict",
    "timing_verdict",
    "overall_verdict",
    "pricing_status",
    "priced_in",
    "not_priced_in",
    "next_signal",
    "invalidation",
    "primary_company_id",
    "phase_entered_at",
    "last_change_at",
    "status",
    "sort_order",
)


def mark_interrupted_industry_trend_jobs(db: Session) -> None:
    rows = db.scalars(
        select(IndustryTrendGenerationJob).where(IndustryTrendGenerationJob.status.in_(JOB_STATUSES_ACTIVE))
    ).all()
    for row in rows:
        row.status = "failed"
        row.phase = "服务重启，任务已中断"
        row.error_message = "后台服务重启后，原Codex任务无法继续。请重新生成。"
        row.finished_at = now_utc()
    if rows:
        db.commit()


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _overall(direction: str, stock: str, timing: str) -> str:
    values = {direction, stock, timing}
    if "否决" in values:
        return "否决"
    if "观察" in values:
        return "观察"
    return "通过"


def _choice(value: str, options: list[str], label: str) -> str:
    if value not in options:
        raise HTTPException(status_code=400, detail=f"{label}不正确")
    return value


def _chain_or_404(db: Session, chain_id: int) -> IndustryChain:
    row = db.get(IndustryChain, chain_id)
    if not row:
        raise HTTPException(status_code=404, detail="产业不存在")
    return row


def _node_or_404(db: Session, chain_id: int, node_id: int) -> IndustryTrendNode:
    row = db.scalar(select(IndustryTrendNode).where(IndustryTrendNode.id == node_id, IndustryTrendNode.chain_id == chain_id))
    if not row:
        raise HTTPException(status_code=404, detail="产业节点不存在")
    return row


def _company_or_404(db: Session, chain_id: int, company_id: int) -> IndustryChainCompany:
    row = db.scalar(select(IndustryChainCompany).where(IndustryChainCompany.id == company_id, IndustryChainCompany.chain_id == chain_id))
    if not row:
        raise HTTPException(status_code=404, detail="产业股票不存在")
    return row


def _task_or_404(db: Session, chain_id: int, task_id: int) -> IndustryChainTask:
    row = db.scalar(select(IndustryChainTask).where(IndustryChainTask.id == task_id, IndustryChainTask.chain_id == chain_id))
    if not row:
        raise HTTPException(status_code=404, detail="验证指标不存在")
    return row


def _update_or_404(db: Session, chain_id: int, update_id: int) -> IndustryTrendUpdate:
    row = db.scalar(select(IndustryTrendUpdate).where(IndustryTrendUpdate.id == update_id, IndustryTrendUpdate.chain_id == chain_id))
    if not row:
        raise HTTPException(status_code=404, detail="跟踪记录不存在")
    return row


def _catalyst_or_404(db: Session, chain_id: int, catalyst_id: int) -> IndustryTrendCatalyst:
    row = db.scalar(
        select(IndustryTrendCatalyst).where(
            IndustryTrendCatalyst.id == catalyst_id,
            IndustryTrendCatalyst.chain_id == chain_id,
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="关键催化事件不存在")
    return row


def _node_out(row: IndustryTrendNode) -> dict[str, Any]:
    return {
        "id": row.id,
        "chain_id": row.chain_id,
        "name": row.name,
        "node_type": row.node_type,
        "plain_explanation": row.plain_explanation,
        "value_flow": row.value_flow,
        "watch_signal": row.watch_signal,
        "maturity_status": row.maturity_status,
        "market_space": row.market_space,
        "tech_barrier": row.tech_barrier,
        "competition": row.competition,
        "profit_elasticity": row.profit_elasticity,
        "localization": row.localization,
        "investment_importance": row.investment_importance,
        "sort_order": row.sort_order,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _edge_out(row: IndustryTrendEdge) -> dict[str, Any]:
    return {"id": row.id, "chain_id": row.chain_id, "from_node_id": row.from_node_id, "to_node_id": row.to_node_id}


def _market_snapshot_from_bars(bars: list[StockDailyBar]) -> dict[str, Any] | None:
    if not bars:
        return None
    latest = bars[0]
    def period_return(period: int) -> float | None:
        if len(bars) <= period or float(bars[period].close) <= 0:
            return None
        return round((float(latest.close) / float(bars[period].close) - 1) * 100, 2)

    recent_amounts = [float(row.amount) for row in bars[:5] if row.amount is not None and float(row.amount) > 0]
    baseline_amounts = [float(row.amount) for row in bars[5:25] if row.amount is not None and float(row.amount) > 0]
    amount_ratio = None
    if recent_amounts and baseline_amounts:
        recent_average = sum(recent_amounts) / len(recent_amounts)
        baseline_average = sum(baseline_amounts) / len(baseline_amounts)
        if baseline_average > 0:
            amount_ratio = round(recent_average / baseline_average, 2)
    recent_high = max(float(row.high) for row in bars[:20])
    distance_from_high = round((float(latest.close) / recent_high - 1) * 100, 2) if recent_high > 0 else None
    return {
        "trade_date": latest.trade_date,
        "close": round(float(latest.close), 4),
        "change_pct": round(float(latest.change_pct), 2) if latest.change_pct is not None else None,
        "amount": round(float(latest.amount), 2) if latest.amount is not None else None,
        "return_5d_pct": period_return(5),
        "return_20d_pct": period_return(20),
        "return_60d_pct": period_return(60),
        "amount_ratio_5d": amount_ratio,
        "distance_from_20d_high_pct": distance_from_high,
        "bars_available": len(bars),
    }


def _market_snapshots(
    db: Session,
    companies: list[IndustryChainCompany],
) -> dict[int, dict[str, Any] | None]:
    company_ids_by_code: dict[str, list[int]] = {}
    for row in companies:
        if row.full_code and (row.market or "A股") == "A股":
            company_ids_by_code.setdefault(row.full_code, []).append(row.id)
    if not company_ids_by_code:
        return {}
    rows = list(
        db.scalars(
            select(StockDailyBar)
            .where(
                StockDailyBar.full_code.in_(company_ids_by_code),
                StockDailyBar.trade_date >= date.today() - timedelta(days=180),
            )
            .order_by(StockDailyBar.full_code, desc(StockDailyBar.trade_date))
        ).all()
    )
    bars_by_code: dict[str, list[StockDailyBar]] = {}
    for row in rows:
        bucket = bars_by_code.setdefault(row.full_code, [])
        if len(bucket) < 61:
            bucket.append(row)
    result: dict[int, dict[str, Any] | None] = {}
    for full_code, company_ids in company_ids_by_code.items():
        snapshot = _market_snapshot_from_bars(bars_by_code.get(full_code, []))
        for company_id in company_ids:
            result[company_id] = snapshot
    return result


def _market_cap_snapshots(
    db: Session,
    companies: list[IndustryChainCompany],
    market_snapshots: dict[int, dict[str, Any] | None],
) -> dict[int, dict[str, Any] | None]:
    """Return an explainable current market-cap anchor for A-share companies.

    The stored market-cap table can be older than the daily price table.  When
    that happens, adjust the stored capitalization by the price change from the
    nearest trading day at the snapshot date.  The response labels the result
    as an estimate so the UI never presents it as a live exchange value.
    """
    by_code = {
        row.full_code: row
        for row in companies
        if row.full_code and (row.market or "A股") == "A股"
    }
    if not by_code:
        return {}
    cap_rows = {
        row.full_code: row
        for row in db.scalars(
            select(FactorMarketCapSnapshot).where(FactorMarketCapSnapshot.full_code.in_(list(by_code)))
        ).all()
    }
    result: dict[int, dict[str, Any] | None] = {}
    for full_code, company in by_code.items():
        cap = cap_rows.get(full_code)
        if not cap or cap.total_market_cap is None:
            result[company.id] = None
            continue
        current_market = market_snapshots.get(company.id)
        total_market_cap = float(cap.total_market_cap)
        float_market_cap = float(cap.float_market_cap) if cap.float_market_cap is not None else None
        # Older Tencent snapshots were stored with fields 44/45 reversed.
        # Correct them on read so existing data remains usable before refresh.
        if cap.source == "tencent" and float_market_cap is not None and float_market_cap > total_market_cap:
            total_market_cap, float_market_cap = float_market_cap, total_market_cap
        as_of = cap.trade_date
        method = "市值快照"
        is_estimated = False
        if cap.trade_date and current_market and current_market.get("close") and current_market.get("trade_date"):
            anchor_bar = db.scalar(
                select(StockDailyBar)
                .where(StockDailyBar.full_code == full_code, StockDailyBar.trade_date <= cap.trade_date)
                .order_by(desc(StockDailyBar.trade_date))
                .limit(1)
            )
            if anchor_bar and anchor_bar.close and float(anchor_bar.close) > 0 and current_market["trade_date"] > anchor_bar.trade_date:
                price_ratio = float(current_market["close"]) / float(anchor_bar.close)
                if 0.25 <= price_ratio <= 4:
                    total_market_cap *= price_ratio
                    if float_market_cap is not None:
                        float_market_cap *= price_ratio
                    as_of = current_market["trade_date"]
                    method = "市值快照按最新收盘价推算"
                    is_estimated = True
        result[company.id] = {
            "total_market_cap": round(total_market_cap, 2),
            "float_market_cap": round(float_market_cap, 2) if float_market_cap is not None else None,
            "as_of": as_of,
            "source": cap.source,
            "method": method,
            "is_estimated": is_estimated,
            "base_market_cap": total_market_cap / price_ratio if is_estimated else total_market_cap,
            "base_date": cap.trade_date,
        }
    return result


def _industry_keywords(chain: IndustryChain, nodes: list[IndustryTrendNode], companies: list[IndustryChainCompany]) -> list[str]:
    keywords: list[str] = []

    def add(value: str | None) -> None:
        clean = (value or "").strip()
        if len(clean) >= 2 and clean.lower() not in {item.lower() for item in keywords}:
            keywords.append(clean)

    for token in re.findall(r"[A-Za-z0-9.]+|[\u4e00-\u9fff]{2,}", chain.name):
        if token.lower() not in {"ai", "数据中心", "服务器", "产业"}:
            add(token)
    if "光" in chain.name:
        for token in ("光通信", "光模块", "光互联", "CPO", "NPO", "1.6T", "800G"):
            add(token)
    if "PCB" in chain.name.upper():
        for token in ("PCB", "CCL", "覆铜板", "电子布", "电子纱", "高速交换"):
            add(token)
    for row in nodes:
        if row.investment_importance == "强":
            add(row.name)
    for row in companies:
        if (row.market or "A股") == "A股":
            add(row.name)
            add(row.code)
    return keywords[:48]


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _shared_demand_labels(chain: IndustryChain) -> set[str]:
    """Return demand-side labels shared by sibling AI infrastructure industries.

    Information screening often classifies a cloud-capex survey as ``企业AI`` or
    ``云计算`` instead of naming every downstream component.  Treat those labels as
    a common demand signal for AI-infrastructure chains, while requiring at least
    two labels below before an automatic association is accepted.
    """
    chain_context = " ".join(
        filter(
            None,
            (
                chain.name,
                chain.summary,
                chain.investment_logic,
                chain.change_summary,
                chain.why_now,
            ),
        )
    ).lower()
    if "ai" in chain_context and any(token in chain_context for token in ("数据中心", "服务器", "算力")):
        return {"企业ai", "云计算", "ai算力", "数据中心", "服务器", "云厂商", "资本开支"}
    return set()


def _information_association(
    row: InformationScreeningItem,
    chain: IndustryChain,
    nodes: list[IndustryTrendNode],
    companies: list[IndustryChainCompany],
) -> dict[str, Any]:
    """Build an automatic, explainable link from an information card to one industry.

    The link is derived on every read so edits, verification changes and deletions in
    information screening are reflected immediately without copying stale material.
    """
    if row.bucket == "filtered" or row.verification_status == "disproved":
        return {"matched": False, "score": 0, "method": "excluded", "reasons": [], "nodes": [], "companies": []}

    searchable = " ".join(
        filter(
            None,
            (
                row.title,
                row.summary,
                row.marginal_change,
                row.evidence_summary,
                row.related_sectors_json,
                row.related_stocks_json,
            ),
        )
    ).lower()
    related_sectors = [str(item).strip().lower() for item in _json_load(row.related_sectors_json, []) if str(item).strip()]
    related_stocks = [str(item).strip().lower() for item in _json_load(row.related_stocks_json, []) if str(item).strip()]
    chain_short = chain.name.split("（", 1)[0].strip().lower()
    reasons: list[str] = []
    score = 0

    explicit = row.promoted_chain_id == chain.id
    if explicit:
        score = 100
        reasons.append("历史明确关联")

    sector_labels = [item for item in related_sectors if item and (item in chain.name.lower() or chain_short in item)]
    if sector_labels:
        score += 8
        reasons.append(f"产业字段：{'、'.join(sector_labels[:2])}")

    shared_demand_matches = [item for item in related_sectors if item in _shared_demand_labels(chain)]
    if len(shared_demand_matches) >= 2:
        score += min(6, len(shared_demand_matches) * 2)
        reasons.append(f"共同需求：{'、'.join(shared_demand_matches[:3])}")

    matched_companies: list[IndustryChainCompany] = []
    matched_node_ids: set[int] = set()
    for company in companies:
        aliases = [company.name, company.code, company.full_code]
        aliases = [str(item).strip().lower() for item in aliases if item and len(str(item).strip()) >= 3]
        if any(alias in searchable or any(alias in stock for stock in related_stocks) for alias in aliases):
            matched_companies.append(company)
            matched_node_ids.update(item for item in _json_load(company.node_ids_json, []) if isinstance(item, int))
    if matched_companies:
        score += min(15, len(matched_companies) * 5)
        reasons.append(f"公司命中：{'、'.join(item.name for item in matched_companies[:3])}")

    directly_matched_nodes = [node for node in nodes if len(node.name.strip()) >= 3 and node.name.strip().lower() in searchable]
    matched_node_ids.update(node.id for node in directly_matched_nodes)
    matched_nodes = [node for node in nodes if node.id in matched_node_ids]
    if matched_nodes:
        score += min(12, len(matched_nodes) * 4)
        reasons.append(f"节点命中：{'、'.join(item.name for item in matched_nodes[:3])}")

    excluded_tokens = {
        chain_short,
        *(item.name.strip().lower() for item in nodes),
        *(item.name.strip().lower() for item in companies),
        *(str(item.code or "").strip().lower() for item in companies),
    }
    thematic_matches = []
    for keyword in _industry_keywords(chain, nodes, companies):
        token = keyword.strip().lower()
        if len(token) < 3 or token in excluded_tokens or token in {"数据中心", "服务器", "产业"}:
            continue
        if token in searchable and token not in thematic_matches:
            thematic_matches.append(token)
    if chain_short and chain_short in searchable and chain_short not in thematic_matches:
        thematic_matches.insert(0, chain_short)
    if thematic_matches:
        score += min(9, len(thematic_matches) * 3)
        reasons.append(f"产业关键词：{'、'.join(thematic_matches[:3])}")

    matched = explicit or score >= 3
    method = "explicit" if explicit else "structured" if sector_labels or related_stocks else "automatic"
    return {
        "matched": matched,
        "score": score,
        "method": method,
        "reasons": reasons,
        "nodes": matched_nodes if matched else [],
        "companies": matched_companies if matched else [],
    }


def _information_auto_tier(row: InformationScreeningItem) -> tuple[str, str]:
    if row.bucket == "verified" and row.verification_status in {"verified", "cross_verified"}:
        return "硬证据", "已自动计入产业硬证据"
    if row.bucket == "verified" and row.verification_status == "partial":
        return "部分验证", "已自动计入产业支持证据"
    return "待验证线索", "已自动进入产业观察层，不改变正式判断"


def _attention_channel(source_type: str | None) -> str:
    value = (source_type or "").strip().lower()
    if value == "xueqiu":
        return "雪球"
    if value in {"announcement", "official", "sec_filing", "regulator", "regulation", "official_website", "official_news", "official_data", "official_cross_check"}:
        return "正式披露"
    if value in {"zsxq", "zsxq_attachment", "zsxq_image", "foreign_research", "research_opinion"}:
        return "专业研究"
    if value in {"market_tape", "market_data"}:
        return "市场行情"
    return "新闻媒体"


def _attention_status(
    current_count: int,
    previous_count: int,
    source_count: int,
    channel_count: int,
    baseline_complete: bool,
) -> tuple[str, str]:
    """只判断信息是否扩散，不把少量社交样本冒充全市场关注度。"""
    if current_count < 5 or source_count < 3 or channel_count < 2:
        return "样本不足", "不足"
    if not baseline_complete:
        return "基线待建立", "待建立"
    if current_count >= previous_count + 3 and (previous_count == 0 or current_count >= previous_count * 1.25):
        return "信息扩散", "可用"
    if previous_count >= 5 and current_count <= previous_count * 0.7:
        return "信息降温", "可用"
    return "信息平稳", "可用"


def _industry_dynamics(
    db: Session,
    chain: IndustryChain,
    nodes: list[IndustryTrendNode],
    companies: list[IndustryChainCompany],
    market_snapshots: dict[int, dict[str, Any] | None],
    updates: list[IndustryTrendUpdate],
    validations: list[IndustryChainTask],
    xq_rows: list[XueqiuPost] | None = None,
    information_rows: list[InformationScreeningItem] | None = None,
) -> dict[str, Any]:
    today = date.today()
    # SQLite以无时区时间保存抓取记录；这里统一按本地库口径比较，避免混用aware/naive datetime。
    now = datetime.now()
    keywords = _industry_keywords(chain, nodes, companies)
    primary = next((row for row in companies if row.id == chain.primary_company_id or row.is_primary), None)
    market_pairs = [(row, market_snapshots.get(row.id)) for row in companies if market_snapshots.get(row.id)]
    market_rows = [snapshot for _, snapshot in market_pairs if snapshot]
    market_total = len(market_pairs)

    def market_weight(company: IndustryChainCompany) -> int:
        if company.id == (primary.id if primary else None) or company.tracking_status == "核心受益":
            return 2
        return 1

    def breadth(field: str) -> float | None:
        values = [
            (float(snapshot[field]), market_weight(company))
            for company, snapshot in market_pairs
            if snapshot and snapshot.get(field) is not None
        ]
        total_weight = sum(weight for _, weight in values)
        if not total_weight:
            return None
        return round(sum(weight for value, weight in values if value > 0) / total_weight * 100, 1)

    amount_values = [
        (float(snapshot["amount_ratio_5d"]), market_weight(company))
        for company, snapshot in market_pairs
        if snapshot and snapshot.get("amount_ratio_5d") is not None
    ]
    amount_weight = sum(weight for _, weight in amount_values)
    amount_expansion = (
        round(sum(weight for value, weight in amount_values if value >= 1.1) / amount_weight * 100, 1)
        if amount_weight else None
    )
    return_20d_values = [float(row["return_20d_pct"]) for row in market_rows if row.get("return_20d_pct") is not None]
    average_return_20d = round(sum(return_20d_values) / len(return_20d_values), 2) if return_20d_values else None
    breadth_5d = breadth("return_5d_pct")
    freshest_market = max((row["trade_date"] for row in market_rows), default=None)
    market_is_stale = bool(freshest_market and freshest_market < today - timedelta(days=5))
    primary_snapshot = market_snapshots.get(primary.id) if primary else None
    primary_market_confirmed = bool(
        primary_snapshot
        and not market_is_stale
        and primary_snapshot.get("return_5d_pct") is not None
        and float(primary_snapshot["return_5d_pct"]) > 0
        and primary_snapshot.get("amount_ratio_5d") is not None
        and float(primary_snapshot["amount_ratio_5d"]) >= 1.0
    )
    sample_adequate = market_total >= 3
    if breadth_5d is None:
        market_status = "数据不足"
    elif market_is_stale:
        market_status = "行情过期"
    elif not sample_adequate:
        market_status = "样本不足"
    elif breadth_5d >= 60 and (amount_expansion or 0) >= 35:
        market_status = "资金确认"
    elif primary_market_confirmed:
        market_status = "龙头先行"
    elif breadth_5d >= 40:
        market_status = "内部出现分化"
    else:
        market_status = "盘面转弱"

    recent_attention_cutoff = now - timedelta(days=7)
    previous_attention_cutoff = now - timedelta(days=14)
    xq_cutoff = now - timedelta(days=21)
    if xq_rows is None:
        xq_rows = list(db.scalars(select(XueqiuPost).where(XueqiuPost.published_at >= xq_cutoff)).all())
    matched_xq = [row for row in xq_rows if _matches_keywords(row.content or "", keywords)]

    information_cutoff = now - timedelta(days=30)
    if information_rows is None:
        information_rows = list(
            db.scalars(
                select(InformationScreeningItem)
                .where(InformationScreeningItem.published_at >= information_cutoff)
                .order_by(desc(InformationScreeningItem.published_at), desc(InformationScreeningItem.id))
            ).all()
        )
    matched_information: list[InformationScreeningItem] = []
    for row in information_rows:
        association = _information_association(row, chain, nodes, companies)
        if association["matched"]:
            matched_information.append(row)
    recent_information = [row for row in matched_information if row.published_at and row.published_at >= now - timedelta(days=14)]
    # 同一链接或标题只算一条，避免同一材料重复进入信息筛选后放大证据数量。
    unique_information: list[InformationScreeningItem] = []
    seen_information: set[str] = set()
    for row in recent_information:
        evidence_key = (row.official_source_url or row.source_url or row.title).strip().lower()
        if evidence_key in seen_information:
            continue
        seen_information.add(evidence_key)
        unique_information.append(row)
    verified_information = [
        row for row in unique_information
        if row.bucket == "verified" and row.verification_status in {"verified", "cross_verified"}
    ]
    partial_information = [
        row for row in unique_information
        if row.bucket == "verified" and row.verification_status == "partial"
    ]

    recent_xq_rows = [row for row in matched_xq if row.published_at and row.published_at >= recent_attention_cutoff]
    previous_xq_rows = [
        row for row in matched_xq
        if row.published_at and previous_attention_cutoff <= row.published_at < recent_attention_cutoff
    ]
    recent_information_7d = [row for row in unique_information if row.published_at and row.published_at >= recent_attention_cutoff]
    previous_information_7d = [
        row for row in unique_information
        if row.published_at and previous_attention_cutoff <= row.published_at < recent_attention_cutoff
    ]

    def attention_key(source_url: str | None, fallback: str) -> str:
        return (source_url or fallback).strip().lower()

    def combine_attention_rows(
        information: list[InformationScreeningItem],
        xueqiu: list[XueqiuPost],
    ) -> list[tuple[str, str, str]]:
        combined: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for row in information:
            key = attention_key(row.official_source_url or row.source_url, row.title)
            if key in seen:
                continue
            seen.add(key)
            combined.append((_attention_channel(row.source_type), (row.source_name or "信息筛选").strip(), key))
        for row in xueqiu:
            key = attention_key(row.source_url, f"xueqiu-{row.id}")
            if key in seen:
                continue
            seen.add(key)
            combined.append(("雪球", (row.author_name or "雪球用户").strip(), key))
        return combined

    recent_attention_rows = combine_attention_rows(recent_information_7d, recent_xq_rows)
    previous_attention_rows = combine_attention_rows(previous_information_7d, previous_xq_rows)
    recent_attention_sources = {source.lower() for _, source, _ in recent_attention_rows if source}
    recent_attention_channels = {channel for channel, _, _ in recent_attention_rows}
    current_uses_information = bool(recent_information_7d)
    current_uses_xueqiu = bool(recent_xq_rows)
    information_history_start = min((row.published_at for row in information_rows if row.published_at), default=None)
    xueqiu_history_start = min((row.published_at for row in xq_rows if row.published_at), default=None)
    baseline_complete = bool(
        (not current_uses_information or (information_history_start and information_history_start <= previous_attention_cutoff))
        and (not current_uses_xueqiu or (xueqiu_history_start and xueqiu_history_start <= previous_attention_cutoff))
    )
    attention_status, attention_coverage = _attention_status(
        len(recent_attention_rows),
        len(previous_attention_rows),
        len(recent_attention_sources),
        len(recent_attention_channels),
        baseline_complete,
    )
    recent_xq = len(recent_xq_rows)
    previous_xq = len(previous_xq_rows)
    recent_xq_authors = len({row.author_name.strip().lower() for row in recent_xq_rows if row.author_name.strip()})

    active_hard_facts = [
        row for row in updates
        if row.causal_stage == "真实变化"
        and row.evidence_type == "硬事实"
        and row.signal_status not in {"失效", "减弱"}
        and row.update_date >= today - timedelta(days=120)
    ]
    recent_hard_facts = [row for row in active_hard_facts if row.update_date >= today - timedelta(days=60)]
    older_hard_facts = [row for row in active_hard_facts if row.update_date < today - timedelta(days=60)]
    confirmed_evidence_count = len(recent_hard_facts) + len(verified_information)
    supporting_evidence_count = len(older_hard_facts) + len(partial_information)
    evidence_count = confirmed_evidence_count + supporting_evidence_count
    if confirmed_evidence_count:
        evidence_status = "硬变化已确认"
    elif supporting_evidence_count:
        evidence_status = "有线索待互证"
    else:
        evidence_status = "等待硬证据"

    evidence_items: list[dict[str, Any]] = []
    for row in recent_hard_facts:
        evidence_items.append(
            {
                "id": f"update-{row.id}",
                "category": "硬事实",
                "title": row.content,
                "summary": row.impact or row.market_response or "",
                "source_name": row.source_name or "产业跟踪记录",
                "source_url": row.source_url,
                "published_at": row.update_date,
                "status": row.signal_status or "已确认",
            }
        )
    for row in verified_information:
        evidence_items.append(
            {
                "id": f"information-{row.id}",
                "category": "信息互证",
                "title": row.title,
                "summary": row.marginal_change or row.evidence_summary or row.summary,
                "source_name": row.source_name or "信息筛选",
                "source_url": row.official_source_url or row.source_url,
                "published_at": row.published_at,
                "status": row.verification_status,
            }
        )
    for row in older_hard_facts:
        evidence_items.append(
            {
                "id": f"update-{row.id}",
                "category": "旧硬事实",
                "title": row.content,
                "summary": row.impact or row.market_response or "",
                "source_name": row.source_name or "产业跟踪记录",
                "source_url": row.source_url,
                "published_at": row.update_date,
                "status": row.signal_status or "待复核",
            }
        )
    for row in partial_information:
        evidence_items.append(
            {
                "id": f"information-{row.id}",
                "category": "部分验证",
                "title": row.title,
                "summary": row.marginal_change or row.evidence_summary or row.summary,
                "source_name": row.source_name or "信息筛选",
                "source_url": row.official_source_url or row.source_url,
                "published_at": row.published_at,
                "status": row.verification_status,
            }
        )
    evidence_items.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)

    attention_items: list[dict[str, Any]] = []
    attention_item_keys: set[str] = set()
    for row in matched_xq:
        if not row.published_at or row.published_at < previous_attention_cutoff:
            continue
        is_recent = bool(row.published_at and row.published_at >= recent_attention_cutoff)
        key = attention_key(row.source_url, f"xueqiu-{row.id}")
        if key in attention_item_keys:
            continue
        attention_item_keys.add(key)
        attention_items.append(
            {
                "id": f"xueqiu-{row.id}",
                "category": "近7日雪球" if is_recent else "前7日雪球",
                "title": f"{row.author_name}的雪球发言",
                "summary": row.content,
                "source_name": row.author_name,
                "source_url": row.source_url,
                "published_at": row.published_at,
                "status": "市场线索",
            }
        )
    for row in unique_information:
        key = attention_key(row.official_source_url or row.source_url, row.title)
        if key in attention_item_keys:
            continue
        attention_item_keys.add(key)
        attention_items.append(
            {
                "id": f"information-{row.id}",
                "category": "新增材料",
                "title": row.title,
                "summary": row.marginal_change or row.summary,
                "source_name": row.source_name or "信息筛选",
                "source_url": row.official_source_url or row.source_url,
                "published_at": row.published_at,
                "status": row.verification_status,
            }
        )
    attention_items.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)

    structural_expression_ok = bool(
        primary
        and primary.verification_status == "已确认"
        and primary.pricing_status != "充分定价"
        and primary.benefit_directness == "强"
    )
    expression_ok = structural_expression_ok and primary_market_confirmed
    if expression_ok:
        expression_status = "结构与盘面共振"
    elif structural_expression_ok:
        expression_status = "公司合格，盘面待确认"
    elif primary:
        expression_status = "表达仍需比较"
    else:
        expression_status = "暂无首选公司"

    sell_signals = sorted(
        (
            row for row in updates
            if row.causal_stage and row.sell_pressure and row.signal_status != "失效"
        ),
        key=lambda row: (row.update_date, row.id),
        reverse=True,
    )
    latest_sell_signal = sell_signals[0] if sell_signals else None
    sell_pressure = latest_sell_signal.sell_pressure if latest_sell_signal else "低"
    sell_reason = latest_sell_signal.content if latest_sell_signal else "暂无明确拥挤或兑现卖压记录"
    if latest_sell_signal and latest_sell_signal.counter_evidence:
        sell_reason = f"{sell_reason}；反向提醒：{latest_sell_signal.counter_evidence}"
    if latest_sell_signal and latest_sell_signal.update_date < today - timedelta(days=30) and sell_pressure == "高":
        sell_pressure = "中"
        sell_reason = f"该高卖压记录已超过30天，需重新确认。{sell_reason}"
    if latest_sell_signal and latest_sell_signal.update_date < today - timedelta(days=45) and sell_pressure == "中":
        sell_pressure = "低"
        sell_reason = f"该中等卖压记录已超过45天，未见持续确认。{sell_reason}"
    if (
        not market_is_stale
        and sell_pressure != "高"
        and average_return_20d is not None
        and average_return_20d >= 20
        and (breadth("return_20d_pct") or 0) >= 60
        and (amount_expansion or 0) >= 50
    ):
        sell_pressure = "高"
        sell_reason = "产业股票近20日涨幅与放量比例同时偏高，获利盘和拥挤风险上升"

    failed_validations = [row for row in validations if row.status == "失败"]
    critical_failed_validations = [
        row for row in failed_validations
        if row.priority == "高" and row.updated_at.date() >= today - timedelta(days=60)
    ]
    strong_evidence = confirmed_evidence_count > 0
    market_positive = market_status in {"资金确认", "龙头先行"}
    market_mixed = market_status == "内部出现分化"
    market_weak = market_status == "盘面转弱"

    if market_weak and (sell_pressure == "高" or critical_failed_validations):
        action = "风险收缩"
        action_tone = "negative"
        if sell_pressure == "高":
            action_reason = "硬变化仍在，但盘面转弱且高卖压成立；先防守，等待龙头止跌和资金重新扩散。"
        else:
            action_reason = "关键验证失败并伴随盘面转弱，产业逻辑需要重新确认。"
    elif strong_evidence and market_positive and expression_ok and sell_pressure != "高":
        action = "可进入交易计划"
        action_tone = "positive"
        action_reason = f"硬变化、{market_status}和首选公司交易表达同时成立；多源信息扩散只作辅助确认。"
    elif strong_evidence and (market_positive or market_mixed):
        action = "等待触发"
        action_tone = "watch"
        blockers: list[str] = []
        if not expression_ok:
            blockers.append("首选公司盘面尚未确认")
        if sell_pressure == "高":
            blockers.append("卖压仍高")
        action_reason = f"硬变化成立且盘面已有买盘，但{'、'.join(blockers) or '扩散范围仍不足'}。"
    elif strong_evidence:
        action = "只跟踪，不追"
        action_tone = "watch"
        if market_status in {"数据不足", "样本不足", "行情过期"}:
            action_reason = f"硬变化成立，但{market_status}，暂时无法确认新增资金进入。"
        else:
            action_reason = "硬变化成立，但板块与首选公司尚未确认新增买盘。"
    else:
        action = "信息不足"
        action_tone = "neutral"
        action_reason = "当前只有线索或旧证据，缺少近期硬事实与独立互证，暂不根据叙事行动。"

    freshest_xq = max((row.published_at for row in xq_rows if row.published_at), default=None)
    freshest_information = max((row.published_at for row in information_rows if row.published_at), default=None)
    conditions = [
        {
            "key": "evidence",
            "label": "真实变化",
            "state": "pass" if strong_evidence else "watch",
            "summary": evidence_status,
        },
        {
            "key": "attention",
            "label": "认知扩散",
            "state": "pass" if attention_status == "信息扩散" else "watch",
            "summary": f"{attention_status}（多源辅助项，不单独否决）",
        },
        {
            "key": "market",
            "label": "资金确认",
            "state": "pass" if market_positive else "danger" if market_weak else "watch",
            "summary": market_status,
        },
        {
            "key": "expression",
            "label": "股票表达",
            "state": "pass" if expression_ok else "watch",
            "summary": expression_status,
        },
        {
            "key": "selling",
            "label": "卖压过滤",
            "state": "danger" if sell_pressure == "高" else "watch" if sell_pressure == "中" else "pass",
            "summary": f"{sell_pressure}卖压",
        },
        {
            "key": "validation",
            "label": "关键验证",
            "state": "danger" if critical_failed_validations else "watch" if failed_validations else "pass",
            "summary": f"{len(critical_failed_validations)} 项近期关键失败" if critical_failed_validations else f"{len(failed_validations)} 项一般失败" if failed_validations else "无失败项",
        },
    ]
    return {
        "as_of": freshest_market or today,
        "action": action,
        "action_tone": action_tone,
        "action_reason": action_reason,
        "evidence": {
            "status": evidence_status,
            "count": evidence_count,
            "confirmed": confirmed_evidence_count,
            "supporting": supporting_evidence_count,
            "hard_facts": len(recent_hard_facts),
            "verified_information": len(verified_information),
            "partial_information": len(partial_information),
            "items": evidence_items,
        },
        "attention": {
            "status": attention_status,
            "coverage": attention_coverage,
            "sample_7d": len(recent_attention_rows),
            "sample_previous_7d": len(previous_attention_rows),
            "source_count_7d": len(recent_attention_sources),
            "channel_count_7d": len(recent_attention_channels),
            "baseline_complete": baseline_complete,
            "xq_7d": recent_xq,
            "xq_previous_7d": previous_xq,
            "xq_author_count_7d": recent_xq_authors,
            "information_7d": len(recent_information_7d),
            "information_previous_7d": len(previous_information_7d),
            "information_14d": len(unique_information),
            "items": attention_items,
        },
        "market": {
            "status": market_status,
            "company_count": market_total,
            "breadth_1d_pct": breadth("change_pct"),
            "breadth_5d_pct": breadth_5d,
            "breadth_20d_pct": breadth("return_20d_pct"),
            "amount_expansion_pct": amount_expansion,
            "average_return_20d_pct": average_return_20d,
            "sample_adequate": sample_adequate,
            "is_stale": market_is_stale,
            "primary_confirmed": primary_market_confirmed,
        },
        "expression": {
            "status": expression_status,
            "company": primary.name if primary else None,
            "verified": bool(primary and primary.verification_status == "已确认"),
            "structural_ready": structural_expression_ok,
            "market_confirmed": primary_market_confirmed,
            "return_5d_pct": primary_snapshot.get("return_5d_pct") if primary_snapshot else None,
            "amount_ratio_5d": primary_snapshot.get("amount_ratio_5d") if primary_snapshot else None,
        },
        "selling": {"level": sell_pressure, "reason": sell_reason},
        "decision_trace": {
            "rule": "硬变化成立 → 盘面资金确认 → 首选公司表达 → 卖压与关键证伪过滤；多源信息扩散只作辅助确认",
            "conditions": conditions,
            "passed": [row["label"] for row in conditions if row["state"] == "pass"],
            "waiting": [row["label"] for row in conditions if row["state"] == "watch"],
            "risks": [row["label"] for row in conditions if row["state"] == "danger"],
        },
        "fresh_items": [
            {
                "id": row.id,
                "title": row.title,
                "published_at": row.published_at,
                "source_name": row.source_name,
                "source_url": row.source_url or row.official_source_url,
                "verification_status": row.verification_status,
                "price_status": row.price_status,
            }
            for row in matched_information[:5]
        ],
        "freshness": {"market": freshest_market, "xueqiu": freshest_xq, "information": freshest_information},
    }


def _company_out(
    row: IndustryChainCompany,
    quote: FactorQuoteSnapshot | None = None,
    market_snapshot: dict[str, Any] | None = None,
    market_cap_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "chain_id": row.chain_id,
        "code": row.code,
        "name": row.name,
        "exchange": row.exchange,
        "full_code": row.full_code,
        "market": row.market or "A股",
        "external_url": row.external_url,
        "position": row.position,
        "company_standing": row.company_standing,
        "core_advantage": row.core_logic,
        "benefit_directness": row.benefit_directness,
        "profit_path": row.profit_path,
        "verification_status": row.verification_status,
        "tracking_status": row.tracking_status,
        "pricing_status": row.pricing_status,
        "is_global_leader": row.is_global_leader,
        "is_domestic_alternative": row.is_domestic_alternative,
        "is_primary": row.is_primary,
        "primary_reason": row.primary_reason,
        "node_ids": _json_load(row.node_ids_json, []),
        "main_risk": row.main_risk,
        "market_implied_expectation": row.market_implied_expectation,
        "evidence_based_expectation": row.evidence_based_expectation,
        "expectation_gap_status": row.expectation_gap_status or "无法判断",
        "expectation_gap_reason": row.expectation_gap_reason,
        "expectation_trigger": row.expectation_trigger,
        "expectation_invalidation": row.expectation_invalidation,
        "expectation_as_of": row.expectation_as_of,
        "expectation_anchor_market_cap": row.expectation_anchor_market_cap,
        "expectation_evidence_growth_pct": row.expectation_evidence_growth_pct,
        "expectation_evidence_acceleration_pct": row.expectation_evidence_acceleration_pct,
        "sort_order": row.sort_order,
        "latest_price": quote.latest_price if quote else None,
        "change_pct": quote.change_pct if quote else None,
        "quote_date": quote.trade_date if quote else None,
        "market_snapshot": market_snapshot,
        "market_cap_snapshot": market_cap_snapshot,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _company_recommendations_out(companies: list[IndustryChainCompany]) -> dict[str, list[dict[str, Any]]]:
    tracking_order = {"核心受益": 0, "重点跟踪": 1, "观察": 2, "淘汰": 3}

    def ranked(rows: list[IndustryChainCompany]) -> list[IndustryChainCompany]:
        return sorted(
            rows,
            key=lambda row: (
                row.verification_status != "已确认",
                tracking_order.get(row.tracking_status, 9),
                row.pricing_status == "充分定价",
                row.sort_order,
                row.id,
            ),
        )

    def item(row: IndustryChainCompany) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "market": row.market or "A股",
            "full_code": row.full_code,
            "position": row.position,
            "reason": row.company_standing or row.core_logic or row.primary_reason,
            "verification_status": row.verification_status,
            "pricing_status": row.pricing_status,
        }

    return {
        "global_leaders": [item(row) for row in ranked([row for row in companies if row.is_global_leader])],
        "domestic_alternatives": [item(row) for row in ranked([row for row in companies if row.is_domestic_alternative])],
    }


def _industry_consensus_out(
    companies: list[IndustryChainCompany],
    snapshot: IndustryExpectationSnapshot | None,
) -> dict[str, Any]:
    """Summarize company-level analyst forecasts without calling them a gap."""
    listed = [row for row in companies if (row.exchange or "").upper() != "PRIVATE"]
    stored_summary = _json_load(snapshot.summary_json, {}) if snapshot else {}
    universe = stored_summary.get("universe") or {}
    consistency = stored_summary.get("consistency") or {}
    consensus_count = int(universe.get("consensus_companies") or 0)
    listed_count = int(universe.get("listed_companies") or len(listed))
    growth_status = consistency.get("growth_status") or "一致预期待补"
    revision_status = consistency.get("revision_status") or "修订样本待补"
    summary = (
        f"汇总{consensus_count}/{listed_count}家上市公司的机构一致预期："
        f"{growth_status}，{revision_status}。这是板块预测方向，不代表预期差。"
    )
    return {
        "status": growth_status,
        "listed_count": listed_count,
        "consensus_count": consensus_count,
        "coverage_rate": universe.get("coverage_rate"),
        "growth_status": growth_status,
        "revision_status": revision_status,
        "summary": summary,
        "as_of": snapshot.snapshot_date if snapshot else None,
    }


def _validation_out(row: IndustryChainTask) -> dict[str, Any]:
    return {
        "id": row.id,
        "chain_id": row.chain_id,
        "company_id": row.company_id,
        "node_id": row.node_id,
        "name": row.title,
        "criteria": row.criteria or row.description,
        "current_result": row.current_result or row.conclusion,
        "status": row.status,
        "target_date": row.due_date,
        "source_name": row.source_name,
        "source_url": row.source_url,
        "priority": row.priority,
        "sort_order": row.sort_order,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _update_out(row: IndustryTrendUpdate) -> dict[str, Any]:
    return {
        "id": row.id,
        "chain_id": row.chain_id,
        "update_date": row.update_date,
        "content": row.content,
        "source_name": row.source_name,
        "source_url": row.source_url,
        "impact": row.impact,
        "next_verification": row.next_verification,
        "affects_phase": row.affects_phase,
        "affects_decision": row.affects_decision,
        "phase_suggestion": row.phase_suggestion,
        "decision_suggestion": _json_load(row.decision_suggestion_json, {}),
        "causal_stage": row.causal_stage,
        "evidence_type": row.evidence_type,
        "signal_status": row.signal_status,
        "buyer_group": row.buyer_group,
        "market_response": row.market_response,
        "sell_pressure": row.sell_pressure,
        "counter_evidence": row.counter_evidence,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _catalyst_out(row: IndustryTrendCatalyst) -> dict[str, Any]:
    return {
        "id": row.id,
        "chain_id": row.chain_id,
        "event_name": row.event_name,
        "expected_time": row.expected_time,
        "event_type": row.event_type,
        "impact_node_id": row.impact_node_id,
        "impact_company_id": row.impact_company_id,
        "importance": row.importance,
        "status": row.status,
        "impact": row.impact,
        "source_name": row.source_name,
        "source_url": row.source_url,
        "sort_order": row.sort_order,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _evidence_out(row: IndustryChainEvidence, node_ids: list[int] | None = None) -> dict[str, Any]:
    state = row.evidence_state or "有效"
    if state == "有效" and row.valid_until and row.valid_until < date.today():
        state = "待复核"
    return {
        "id": row.id,
        "company_id": row.company_id,
        "node_ids": node_ids or [],
        "title": row.title,
        "content": row.content,
        "source_name": row.source_name,
        "source_url": row.source_url,
        "impact_level": row.impact_level,
        "source_tier": row.source_tier,
        "verification_status": row.verification_status,
        "evidence_state": state,
        "valid_until": row.valid_until,
        "conflict_note": row.conflict_note,
        "evidence_date": row.evidence_date,
    }


def _material_out(row: IndustryTrendMaterial) -> dict[str, Any]:
    return {
        "id": row.id,
        "chain_id": row.chain_id,
        "title": row.title,
        "content": row.content,
        "source_type": row.source_type,
        "source_name": row.source_name,
        "source_url": row.source_url,
        "material_date": row.material_date,
        "change_type": row.change_type,
        "status": row.status,
        "note": row.note,
        "processed_at": row.processed_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _chain_core_out(row: IndustryChain) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "summary": row.summary,
        "phase": row.phase,
        "strength": row.strength,
        "catalyst": row.catalyst,
        "risk": row.risk,
        "investment_logic": row.investment_logic,
        "change_summary": row.change_summary,
        "why_now": row.why_now,
        "drivers": _json_load(row.drivers_json, []),
        "expected_duration": row.expected_duration,
        "attention_level": row.attention_level,
        "direction_verdict": row.direction_verdict,
        "stock_verdict": row.stock_verdict,
        "timing_verdict": row.timing_verdict,
        "overall_verdict": row.overall_verdict,
        "pricing_status": row.pricing_status,
        "priced_in": row.priced_in,
        "not_priced_in": row.not_priced_in,
        "next_signal": row.next_signal,
        "invalidation": row.invalidation,
        "primary_company_id": row.primary_company_id,
        "phase_entered_at": row.phase_entered_at,
        "last_change_at": row.last_change_at,
        "revision": row.revision,
        "status": row.status,
        "sort_order": row.sort_order,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _detail_out(db: Session, chain: IndustryChain, include_dynamics: bool = True) -> dict[str, Any]:
    nodes = list(db.scalars(select(IndustryTrendNode).where(IndustryTrendNode.chain_id == chain.id).order_by(IndustryTrendNode.sort_order, IndustryTrendNode.id)))
    edges = list(db.scalars(select(IndustryTrendEdge).where(IndustryTrendEdge.chain_id == chain.id).order_by(IndustryTrendEdge.id)))
    companies = list(db.scalars(select(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain.id).order_by(IndustryChainCompany.sort_order, IndustryChainCompany.id)))
    quotes: dict[str, FactorQuoteSnapshot] = {}
    full_codes = [row.full_code for row in companies if row.full_code and (row.market or "A股") == "A股"]
    if full_codes:
        quotes = {row.full_code: row for row in db.scalars(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code.in_(full_codes))).all()}
    validations = list(db.scalars(select(IndustryChainTask).where(IndustryChainTask.chain_id == chain.id).order_by(IndustryChainTask.sort_order, IndustryChainTask.id)))
    catalysts = list(db.scalars(select(IndustryTrendCatalyst).where(IndustryTrendCatalyst.chain_id == chain.id).order_by(IndustryTrendCatalyst.sort_order, IndustryTrendCatalyst.id)))
    updates = list(db.scalars(select(IndustryTrendUpdate).where(IndustryTrendUpdate.chain_id == chain.id).order_by(desc(IndustryTrendUpdate.update_date), desc(IndustryTrendUpdate.id))))
    evidence = list(db.scalars(select(IndustryChainEvidence).where(IndustryChainEvidence.chain_id == chain.id).order_by(desc(IndustryChainEvidence.evidence_date), desc(IndustryChainEvidence.id))))
    source_links = list(db.scalars(select(IndustryTrendNodeSource).where(IndustryTrendNodeSource.chain_id == chain.id).order_by(IndustryTrendNodeSource.id)))
    materials = list(
        db.scalars(
            select(IndustryTrendMaterial)
            .where(IndustryTrendMaterial.chain_id == chain.id)
            .order_by(desc(IndustryTrendMaterial.material_date), desc(IndustryTrendMaterial.id))
            .limit(100)
        )
    )
    node_ids_by_evidence: dict[int, list[int]] = {}
    for link in source_links:
        node_ids_by_evidence.setdefault(link.evidence_id, []).append(link.node_id)
    draft = db.scalar(select(IndustryTrendDraft).where(IndustryTrendDraft.chain_id == chain.id))
    primary = next((row for row in companies if row.id == chain.primary_company_id or row.is_primary), None)
    market_snapshots = _market_snapshots(db, companies)
    market_cap_snapshots = _market_cap_snapshots(db, companies, market_snapshots)
    primary_snapshot = market_snapshots.get(primary.id) if primary else None
    next_catalyst = next((row for row in catalysts if row.status != "兑现"), None)
    dynamics = _industry_dynamics(db, chain, nodes, companies, market_snapshots, updates, validations) if include_dynamics else None
    result = {
        **_chain_core_out(chain),
        "nodes": [_node_out(row) for row in nodes],
        "edges": [_edge_out(row) for row in edges],
        "companies": [
            _company_out(
                row,
                quotes.get(row.full_code or ""),
                market_snapshots.get(row.id),
                market_cap_snapshots.get(row.id),
            )
            for row in companies
        ],
        "company_recommendations": _company_recommendations_out(companies),
        "validations": [_validation_out(row) for row in validations],
        "catalysts": [_catalyst_out(row) for row in catalysts],
        "updates": [_update_out(row) for row in updates],
        "sources": [_evidence_out(row, node_ids_by_evidence.get(row.id, [])) for row in evidence],
        "materials": [_material_out(row) for row in materials],
        "validation_summary": {
            "total": len(validations),
            "confirmed": sum(item.status == "已确认" for item in validations),
            "in_progress": sum(item.status == "验证中" for item in validations),
            "failed": sum(item.status == "失败" for item in validations),
        },
        "next_catalyst": _catalyst_out(next_catalyst) if next_catalyst else None,
        "pending_material_count": sum(item.status == "待处理" for item in materials),
        "market_snapshot": primary_snapshot,
        **({"dynamics": dynamics} if include_dynamics else {}),
        "primary_company": _company_out(
            primary,
            quotes.get(primary.full_code or ""),
            primary_snapshot,
            market_cap_snapshots.get(primary.id),
        ) if primary else None,
        "draft": {
            "id": draft.id,
            "draft_type": draft.draft_type,
            "source": draft.source,
            "payload": _json_load(draft.payload_json, {}),
            "updated_at": draft.updated_at,
        } if draft else None,
    }
    result["decision_policy"] = evaluate_industry_policies(result, result.get("primary_company"))
    return result


def _snapshot(db: Session, chain: IndustryChain) -> dict[str, Any]:
    detail = _detail_out(db, chain, include_dynamics=False)
    detail.pop("draft", None)
    detail.pop("primary_company", None)
    return detail


def _save_version(db: Session, chain: IndustryChain) -> None:
    chain.overall_verdict = _overall(chain.direction_verdict, chain.stock_verdict, chain.timing_verdict)
    chain.revision += 1
    chain.updated_at = now_utc()
    db.flush()
    db.add(IndustryTrendVersion(chain_id=chain.id, revision=chain.revision, snapshot_json=_json_dump(_snapshot(db, chain))))


class TrendCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    summary: str | None = None
    attention_level: str = "观察"


class TrendPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    summary: str | None = None
    phase: str | None = None
    strength: int | None = Field(default=None, ge=0, le=100)
    catalyst: str | None = None
    risk: str | None = None
    investment_logic: str | None = None
    change_summary: str | None = None
    why_now: str | None = None
    drivers: list[str] | None = None
    expected_duration: str | None = Field(default=None, max_length=120)
    attention_level: str | None = None
    direction_verdict: str | None = None
    stock_verdict: str | None = None
    timing_verdict: str | None = None
    pricing_status: str | None = None
    priced_in: str | None = None
    not_priced_in: str | None = None
    next_signal: str | None = None
    invalidation: str | None = None
    sort_order: int | None = None


class NodePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    node_type: str = "光互联产品"
    plain_explanation: str | None = None
    value_flow: str | None = None
    watch_signal: str | None = None
    maturity_status: str | None = None
    market_space: str | None = None
    tech_barrier: str | None = None
    competition: str | None = None
    profit_elasticity: str | None = None
    localization: str | None = None
    investment_importance: str | None = None
    sort_order: int = 100


class NodePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    node_type: str | None = None
    plain_explanation: str | None = None
    value_flow: str | None = None
    watch_signal: str | None = None
    maturity_status: str | None = None
    market_space: str | None = None
    tech_barrier: str | None = None
    competition: str | None = None
    profit_elasticity: str | None = None
    localization: str | None = None
    investment_importance: str | None = None
    sort_order: int | None = None


class EdgePayload(BaseModel):
    from_node_id: int
    to_node_id: int

    @model_validator(mode="after")
    def validate_nodes(self) -> "EdgePayload":
        if self.from_node_id == self.to_node_id:
            raise ValueError("产业节点不能连接自身")
        return self


class CompanyPayload(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    market: str = "A股"
    exchange: str | None = Field(default=None, max_length=32)
    external_url: str | None = None
    position: str | None = None
    company_standing: str | None = None
    core_advantage: str | None = None
    benefit_directness: str | None = None
    profit_path: str | None = None
    verification_status: str = "未验证"
    tracking_status: str = "观察"
    pricing_status: str = "部分定价"
    is_global_leader: bool = False
    is_domestic_alternative: bool = False
    is_primary: bool = False
    primary_reason: str | None = None
    node_ids: list[int] = Field(default_factory=list)
    main_risk: str | None = None
    market_implied_expectation: str | None = None
    evidence_based_expectation: str | None = None
    expectation_gap_status: str = "无法判断"
    expectation_gap_reason: str | None = None
    expectation_trigger: str | None = None
    expectation_invalidation: str | None = None
    expectation_as_of: date | None = None
    expectation_anchor_market_cap: float | None = Field(default=None, ge=0)
    expectation_evidence_growth_pct: float | None = None
    expectation_evidence_acceleration_pct: float | None = None
    sort_order: int = 100


class CompanyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    external_url: str | None = None
    position: str | None = None
    company_standing: str | None = None
    core_advantage: str | None = None
    benefit_directness: str | None = None
    profit_path: str | None = None
    verification_status: str | None = None
    tracking_status: str | None = None
    pricing_status: str | None = None
    is_global_leader: bool | None = None
    is_domestic_alternative: bool | None = None
    is_primary: bool | None = None
    primary_reason: str | None = None
    node_ids: list[int] | None = None
    main_risk: str | None = None
    market_implied_expectation: str | None = None
    evidence_based_expectation: str | None = None
    expectation_gap_status: str | None = None
    expectation_gap_reason: str | None = None
    expectation_trigger: str | None = None
    expectation_invalidation: str | None = None
    expectation_as_of: date | None = None
    expectation_anchor_market_cap: float | None = Field(default=None, ge=0)
    expectation_evidence_growth_pct: float | None = None
    expectation_evidence_acceleration_pct: float | None = None
    sort_order: int | None = None


class ValidationPayload(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    criteria: str | None = None
    current_result: str | None = None
    status: str = "未验证"
    company_id: int | None = None
    node_id: int | None = None
    target_date: date | None = None
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None
    priority: str = "中"
    sort_order: int = 100


class ValidationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=240)
    criteria: str | None = None
    current_result: str | None = None
    status: str | None = None
    company_id: int | None = None
    node_id: int | None = None
    target_date: date | None = None
    source_name: str | None = None
    source_url: str | None = None
    priority: str | None = None
    sort_order: int | None = None


class CatalystPayload(BaseModel):
    event_name: str = Field(min_length=1, max_length=240)
    expected_time: str | None = Field(default=None, max_length=80)
    event_type: str = Field(default="其他", max_length=40)
    impact_node_id: int | None = None
    impact_company_id: int | None = None
    importance: str = "中"
    status: str = "预期"
    impact: str | None = None
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None
    sort_order: int = 100


class CatalystPatch(BaseModel):
    event_name: str | None = Field(default=None, min_length=1, max_length=240)
    expected_time: str | None = Field(default=None, max_length=80)
    event_type: str | None = Field(default=None, max_length=40)
    impact_node_id: int | None = None
    impact_company_id: int | None = None
    importance: str | None = None
    status: str | None = None
    impact: str | None = None
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None
    sort_order: int | None = None


class UpdatePayload(BaseModel):
    update_date: date = Field(default_factory=date.today)
    content: str = Field(min_length=1)
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None
    impact: str | None = None
    next_verification: str | None = None
    affects_phase: bool = False
    affects_decision: bool = False
    phase_suggestion: str | None = None
    decision_suggestion: dict[str, str] = Field(default_factory=dict)
    causal_stage: str | None = None
    evidence_type: str | None = None
    signal_status: str | None = None
    buyer_group: str | None = Field(default=None, max_length=160)
    market_response: str | None = None
    sell_pressure: str | None = None
    counter_evidence: str | None = None


class UpdatePatch(BaseModel):
    update_date: date | None = None
    content: str | None = Field(default=None, min_length=1)
    source_name: str | None = None
    source_url: str | None = None
    impact: str | None = None
    next_verification: str | None = None
    affects_phase: bool | None = None
    affects_decision: bool | None = None
    phase_suggestion: str | None = None
    decision_suggestion: dict[str, str] | None = None
    causal_stage: str | None = None
    evidence_type: str | None = None
    signal_status: str | None = None
    buyer_group: str | None = Field(default=None, max_length=160)
    market_response: str | None = None
    sell_pressure: str | None = None
    counter_evidence: str | None = None


class DraftPayload(BaseModel):
    draft_type: Literal["initial", "update", "chat"] = "chat"
    source: str = Field(default="codex_chat", max_length=40)
    payload: dict[str, Any]


class GeneratePayload(BaseModel):
    chain_id: int | None = None
    job_type: Literal["initial", "update"] = "initial"
    name: str | None = Field(default=None, max_length=100)
    clue: str | None = None
    focus_stocks: list[str] = Field(default_factory=list)
    requirements: str | None = None


class ResearchSettingPayload(BaseModel):
    priority_nodes: list[str] = Field(default_factory=list)
    priority_companies: list[str] = Field(default_factory=list)
    source_preferences: list[str] = Field(default_factory=lambda: list(DEFAULT_SOURCE_PREFERENCES))
    excluded_keywords: list[str] = Field(default_factory=list)
    evidence_rules: str | None = DEFAULT_EVIDENCE_RULES
    custom_instructions: str | None = None
    token_budget: int = Field(default=24000, ge=4000, le=100000)
    allow_new_nodes: bool = True
    allow_new_companies: bool = True
    draft_only: bool = True


class MaterialCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str | None = None
    source_type: str = Field(default="本地对话", max_length=32)
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None
    material_date: date = Field(default_factory=date.today)
    change_type: str = "新增证据"
    note: str | None = None


class MaterialPatch(BaseModel):
    status: str | None = None
    change_type: str | None = None
    note: str | None = None


class EvidenceStatePatch(BaseModel):
    evidence_state: str
    valid_until: date | None = None
    conflict_note: str | None = None


class IndustryDecisionCreate(BaseModel):
    decision_code: Literal["A", "B", "C", "D"]
    primary_company_id: int | None = None
    planned_horizon: Literal[5, 20, 60] = 20
    cost_bps: int = Field(default=20, ge=0, le=200)
    thesis: str | None = Field(default=None, max_length=2000)
    pricing_verdict: str | None = Field(default=None, max_length=1500)
    why_best: str | None = Field(default=None, max_length=1500)
    trigger_conditions: list[str] = Field(default_factory=list, max_length=20)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=20)


class StrictCodexModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodexNode(StrictCodexModel):
    name: str
    node_type: Literal["需求驱动", "网络与系统", "光互联产品", "核心器件", "制造与配套"] = "光互联产品"
    plain_explanation: str | None = None
    value_flow: str | None = None
    watch_signal: str | None = None
    maturity_status: Literal["前沿储备", "验证中", "小批量", "放量中", "成熟应用"] | None = None
    market_space: str | None = None
    tech_barrier: str | None = None
    competition: str | None = None
    profit_elasticity: Literal["强", "中", "弱"] | None = None
    localization: Literal["强", "中", "弱"] | None = None
    investment_importance: Literal["强", "中", "弱"] | None = None


class CodexCompany(StrictCodexModel):
    code: str
    name: str
    market: Literal["A股", "美股", "台湾", "韩国", "日本", "其他"] = "A股"
    exchange: str | None = None
    external_url: str | None = None
    node_names: list[str] = Field(default_factory=list)
    position: str | None = None
    company_standing: str | None = None
    core_advantage: str | None = None
    benefit_directness: str | None = None
    profit_path: str | None = None
    verification_status: Literal["未验证", "验证中", "已确认", "失败"] = "未验证"
    tracking_status: Literal["核心受益", "重点跟踪", "观察", "淘汰"] = "观察"
    pricing_status: Literal["未定价", "部分定价", "充分定价"] = "部分定价"
    is_global_leader: bool = False
    is_domestic_alternative: bool = False
    is_primary: bool = False
    primary_reason: str | None = None
    main_risk: str | None = None
    market_implied_expectation: str | None = None
    evidence_based_expectation: str | None = None
    expectation_gap_status: Literal["正向预期差", "基本匹配", "负向预期差", "无法判断"] = "无法判断"
    expectation_gap_reason: str | None = None
    expectation_trigger: str | None = None
    expectation_invalidation: str | None = None
    expectation_evidence_growth_pct: float | None = None
    expectation_evidence_acceleration_pct: float | None = None


class CodexValidation(StrictCodexModel):
    name: str
    criteria: str | None = None
    current_result: str | None = None
    status: Literal["未验证", "验证中", "已确认", "失败"] = "未验证"
    company_code: str | None = None
    node_name: str | None = None
    target_date: date | None = None
    source_name: str | None = None
    source_url: str | None = None
    sort_order: int = 100


class CodexCatalyst(StrictCodexModel):
    event_name: str
    expected_time: str | None = None
    event_type: str = "其他"
    impact_node_name: str | None = None
    impact_company_code: str | None = None
    importance: Literal["高", "中", "低"] = "中"
    status: Literal["预期", "确认", "兑现"] = "预期"
    impact: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    sort_order: int = 100


class CodexUpdate(StrictCodexModel):
    update_date: date
    content: str
    source_name: str | None = None
    source_url: str | None = None
    impact: str | None = None
    next_verification: str | None = None
    causal_stage: Literal["真实变化", "认知扩散", "资金进入", "筹码交换", "拥挤退潮"] | None = None
    evidence_type: Literal["硬事实", "市场线索", "盘面确认", "市场推断"] | None = None
    signal_status: Literal["线索", "已确认", "减弱", "失效"] | None = None
    buyer_group: str | None = None
    market_response: str | None = None
    sell_pressure: Literal["低", "中", "高"] | None = None
    counter_evidence: str | None = None


class CodexSource(StrictCodexModel):
    title: str
    content: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    evidence_date: date
    impact_level: Literal["强", "中", "弱"] = "中"
    source_tier: Literal["官方硬证据", "公司披露", "行业标准", "市场数据", "雪球线索", "Codex判断"] = "官方硬证据"
    verification_status: Literal["已互证", "单一来源", "市场线索", "待验证"] = "单一来源"
    node_names: list[str] = Field(default_factory=list)


class CodexEdge(StrictCodexModel):
    from_name: str
    to_name: str


class CodexResult(StrictCodexModel):
    summary: str
    investment_logic: str
    change_summary: str
    why_now: str
    drivers: list[str]
    expected_duration: str | None = None
    risk: str | None = None
    phase: str
    strength: int = Field(ge=0, le=100)
    attention_level: str
    direction_verdict: str
    stock_verdict: str
    timing_verdict: str
    pricing_status: str
    priced_in: str | None = None
    not_priced_in: str | None = None
    next_signal: str | None = None
    invalidation: str | None = None
    update_summary: str | None = None
    invalidated_information: list[str] = Field(default_factory=list)
    phase_change_reason: str | None = None
    stock_ranking_change: str | None = None
    primary_company_assessment: str | None = None
    pricing_change: str | None = None
    validation_changes: list[str] = Field(default_factory=list)
    decision_change_reason: str | None = None
    nodes: list[CodexNode] = Field(default_factory=list)
    edges: list[CodexEdge] = Field(default_factory=list)
    companies: list[CodexCompany] = Field(default_factory=list)
    validations: list[CodexValidation] = Field(default_factory=list)
    catalysts: list[CodexCatalyst] = Field(default_factory=list)
    updates: list[CodexUpdate] = Field(default_factory=list)
    sources: list[CodexSource] = Field(default_factory=list)


class CodexCorePatch(StrictCodexModel):
    summary: str | None = None
    investment_logic: str | None = None
    change_summary: str | None = None
    why_now: str | None = None
    drivers: list[str] | None = None
    expected_duration: str | None = None
    risk: str | None = None
    phase: Literal["观察期", "萌芽期", "验证期", "增长期", "爆发期", "成熟期", "退潮期"] | None = None
    strength: int | None = Field(default=None, ge=0, le=100)
    attention_level: Literal["重点跟踪", "持续跟踪", "观察", "暂停"] | None = None
    direction_verdict: Literal["通过", "观察", "否决"] | None = None
    stock_verdict: Literal["通过", "观察", "否决"] | None = None
    timing_verdict: Literal["通过", "观察", "否决"] | None = None
    pricing_status: Literal["未定价", "部分定价", "充分定价"] | None = None
    priced_in: str | None = None
    not_priced_in: str | None = None
    next_signal: str | None = None
    invalidation: str | None = None


class CodexNodeDelta(StrictCodexModel):
    action: Literal["新增", "更新"] = "更新"
    name: str
    node_type: Literal["需求驱动", "网络与系统", "光互联产品", "核心器件", "制造与配套"] | None = None
    plain_explanation: str | None = None
    value_flow: str | None = None
    watch_signal: str | None = None
    maturity_status: Literal["前沿储备", "验证中", "小批量", "放量中", "成熟应用"] | None = None
    market_space: str | None = None
    tech_barrier: str | None = None
    competition: str | None = None
    profit_elasticity: Literal["强", "中", "弱"] | None = None
    localization: Literal["强", "中", "弱"] | None = None
    investment_importance: Literal["强", "中", "弱"] | None = None


class CodexCompanyDelta(StrictCodexModel):
    action: Literal["新增", "更新"] = "更新"
    name: str
    code: str | None = None
    market: Literal["A股", "美股", "台湾", "韩国", "日本", "其他"] | None = None
    exchange: str | None = None
    external_url: str | None = None
    node_names: list[str] = Field(default_factory=list)
    position: str | None = None
    company_standing: str | None = None
    core_advantage: str | None = None
    benefit_directness: str | None = None
    profit_path: str | None = None
    verification_status: Literal["未验证", "验证中", "已确认", "失败"] | None = None
    tracking_status: Literal["核心受益", "重点跟踪", "观察", "淘汰"] | None = None
    pricing_status: Literal["未定价", "部分定价", "充分定价"] | None = None
    is_global_leader: bool | None = None
    is_domestic_alternative: bool | None = None
    is_primary: bool | None = None
    primary_reason: str | None = None
    main_risk: str | None = None
    market_implied_expectation: str | None = None
    evidence_based_expectation: str | None = None
    expectation_gap_status: Literal["正向预期差", "基本匹配", "负向预期差", "无法判断"] | None = None
    expectation_gap_reason: str | None = None
    expectation_trigger: str | None = None
    expectation_invalidation: str | None = None


class CodexValidationDelta(StrictCodexModel):
    action: Literal["新增", "更新"] = "更新"
    name: str
    criteria: str | None = None
    current_result: str | None = None
    status: Literal["未验证", "验证中", "已确认", "失败"] | None = None
    company_code: str | None = None
    node_name: str | None = None
    target_date: date | None = None
    source_name: str | None = None
    source_url: str | None = None


class CodexCatalystDelta(StrictCodexModel):
    action: Literal["新增", "更新"] = "更新"
    event_name: str
    expected_time: str | None = None
    event_type: str | None = None
    impact_node_name: str | None = None
    impact_company_code: str | None = None
    importance: Literal["高", "中", "低"] | None = None
    status: Literal["预期", "确认", "兑现"] | None = None
    impact: str | None = None
    source_name: str | None = None
    source_url: str | None = None


class CodexCoverageItem(StrictCodexModel):
    area: str
    status: Literal["已更新", "无变化", "证据不足", "存在冲突"]
    finding: str | None = None
    gap: str | None = None


class CodexUpdateDelta(StrictCodexModel):
    delta_version: Literal["1"] = "1"
    no_material_change: bool = False
    update_summary: str
    searched_since: str | None = None
    core_patch: CodexCorePatch = Field(default_factory=CodexCorePatch)
    coverage: list[CodexCoverageItem] = Field(default_factory=list)
    invalidated_information: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    nodes: list[CodexNodeDelta] = Field(default_factory=list)
    edges_add: list[CodexEdge] = Field(default_factory=list)
    companies: list[CodexCompanyDelta] = Field(default_factory=list)
    validations: list[CodexValidationDelta] = Field(default_factory=list)
    catalysts: list[CodexCatalystDelta] = Field(default_factory=list)
    updates: list[CodexUpdate] = Field(default_factory=list)
    new_sources: list[CodexSource] = Field(default_factory=list)


@router.get("")
def list_trends(
    query: str = Query(default="", max_length=100),
    phase: str | None = Query(default=None),
    attention_level: str | None = Query(default=None),
    include_paused: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    statement = select(IndustryChain)
    if not include_paused:
        statement = statement.where(IndustryChain.attention_level != "暂停", IndustryChain.status != "archived")
    if query.strip():
        keyword = f"%{query.strip()}%"
        statement = statement.where(or_(IndustryChain.name.like(keyword), IndustryChain.summary.like(keyword), IndustryChain.investment_logic.like(keyword)))
    if phase:
        statement = statement.where(IndustryChain.phase == _choice(phase, PHASES, "产业阶段"))
    if attention_level:
        statement = statement.where(IndustryChain.attention_level == _choice(attention_level, ATTENTION_LEVELS, "关注等级"))
    attention_order = {"重点跟踪": 0, "持续跟踪": 1, "观察": 2, "暂停": 3}
    rows = list(db.scalars(statement).all())
    rows.sort(key=lambda row: (attention_order.get(row.attention_level, 9), -(row.last_change_at or row.updated_at).timestamp(), -row.strength, row.sort_order, row.id))
    items: list[dict[str, Any]] = []
    for row in rows:
        chain_companies = list(
            db.scalars(
                select(IndustryChainCompany)
                .where(IndustryChainCompany.chain_id == row.id)
                .order_by(IndustryChainCompany.sort_order, IndustryChainCompany.id)
            ).all()
        )
        primary = next((item for item in chain_companies if item.id == row.primary_company_id), None)
        validations = list(db.scalars(select(IndustryChainTask).where(IndustryChainTask.chain_id == row.id)).all())
        next_catalyst = db.scalar(
            select(IndustryTrendCatalyst)
            .where(IndustryTrendCatalyst.chain_id == row.id, IndustryTrendCatalyst.status != "兑现")
            .order_by(IndustryTrendCatalyst.sort_order, IndustryTrendCatalyst.id)
        )
        pending_materials = len(
            list(
                db.scalars(
                    select(IndustryTrendMaterial.id).where(
                        IndustryTrendMaterial.chain_id == row.id,
                        IndustryTrendMaterial.status == "待处理",
                    )
                )
            )
        )
        primary_snapshot = _market_snapshots(db, [primary]).get(primary.id) if primary and (primary.market or "A股") == "A股" else None
        items.append({
            **_chain_core_out(row),
            "primary_company": _company_out(primary, market_snapshot=primary_snapshot) if primary else None,
            "company_recommendations": _company_recommendations_out(chain_companies),
            "validation_summary": {
                "total": len(validations),
                "confirmed": sum(item.status == "已确认" for item in validations),
                "in_progress": sum(item.status == "验证中" for item in validations),
                "failed": sum(item.status == "失败" for item in validations),
            },
            "next_catalyst": _catalyst_out(next_catalyst) if next_catalyst else None,
            "pending_material_count": pending_materials,
            "market_snapshot": primary_snapshot,
        })
    return {"items": items, "phases": PHASES, "attention_levels": ATTENTION_LEVELS}


VALIDATION_ROUTE_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("需求", ("需求", "资本开支", "预算", "出货", "渗透率")),
    ("订单", ("订单", "客户", "认证", "交付", "在手", "产能", "良率")),
    ("价格", ("价格", "报价", "涨价", "单价", "asp")),
    ("利润", ("收入", "利润", "毛利", "业绩", "现金流", "盈利")),
)
AI_CHAIN_KEYWORDS = ("ai", "算力", "数据中心", "光通信", "光互联", "光模块", "pcb", "液冷", "服务器", "高速交换", "cpo")
AI_SHARED_EVIDENCE_KEYWORDS = ("资本开支", "capex", "云厂商", "ai基础设施投入", "ai infrastructure investment")


def _validation_route_stage(row: IndustryChainTask) -> str:
    text = " ".join(filter(None, (row.title, row.criteria, row.description))).lower()
    for stage, keywords in VALIDATION_ROUTE_STAGES:
        if any(keyword in text for keyword in keywords):
            return stage
    return "订单"


def _evidence_merge_key(row: IndustryChainEvidence) -> str:
    if row.source_url and row.source_url.strip():
        return f"url:{row.source_url.strip().lower().rstrip('/')}"
    title = re.sub(r"\s+", "", row.title or "").lower()
    return f"title:{title}"


def _is_ai_chain(row: IndustryChain) -> bool:
    text = " ".join(filter(None, (row.name, row.summary, row.investment_logic, row.change_summary, row.why_now))).lower()
    return any(keyword in text for keyword in AI_CHAIN_KEYWORDS)


@router.get("/intelligence")
def get_industry_intelligence(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Aggregate evidence and market expression across every active industry chain."""
    chains = list(
        db.scalars(
            select(IndustryChain).where(
                IndustryChain.attention_level != "暂停",
                IndustryChain.status != "archived",
            )
        ).all()
    )
    attention_order = {"重点跟踪": 0, "持续跟踪": 1, "观察": 2, "暂停": 3}
    chains.sort(
        key=lambda row: (
            attention_order.get(row.attention_level, 9),
            -(row.last_change_at or row.updated_at).timestamp(),
            -row.strength,
            row.id,
        )
    )
    chain_ids = [row.id for row in chains]
    if not chain_ids:
        return {
            "theme": "A股产业链",
            "overall": {
                "action": "信息不足",
                "tone": "neutral",
                "reason": "尚未建立可跟踪的产业链。",
                "latest_at": None,
                "action_counts": {},
            },
            "summary": {"industries": 0, "sources": 0, "supporting_evidence": 0, "shared_evidence": 0, "risk_evidence": 0, "confirmed_validations": 0, "total_validations": 0, "fresh_changes_7d": 0, "verified_changes_7d": 0, "shared_changes_7d": 0},
            "sectors": [],
            "fresh_changes": [],
            "latest_changes": [],
            "validation_route": [],
            "supporting_evidence": [],
            "risk_signals": [],
        }

    companies = list(
        db.scalars(
            select(IndustryChainCompany)
            .where(IndustryChainCompany.chain_id.in_(chain_ids))
            .order_by(IndustryChainCompany.sort_order, IndustryChainCompany.id)
        ).all()
    )
    companies_by_id = {row.id: row for row in companies}
    companies_by_chain: dict[int, list[IndustryChainCompany]] = {chain_id: [] for chain_id in chain_ids}
    for row in companies:
        companies_by_chain[row.chain_id].append(row)
    nodes = list(
        db.scalars(
            select(IndustryTrendNode)
            .where(IndustryTrendNode.chain_id.in_(chain_ids))
            .order_by(IndustryTrendNode.sort_order, IndustryTrendNode.id)
        ).all()
    )
    nodes_by_chain: dict[int, list[IndustryTrendNode]] = {chain_id: [] for chain_id in chain_ids}
    for row in nodes:
        nodes_by_chain[row.chain_id].append(row)
    market_snapshots = _market_snapshots(db, companies)
    validations = list(
        db.scalars(
            select(IndustryChainTask)
            .where(IndustryChainTask.chain_id.in_(chain_ids))
            .order_by(IndustryChainTask.sort_order, IndustryChainTask.id)
        ).all()
    )
    catalysts = list(
        db.scalars(
            select(IndustryTrendCatalyst)
            .where(IndustryTrendCatalyst.chain_id.in_(chain_ids))
            .order_by(IndustryTrendCatalyst.sort_order, IndustryTrendCatalyst.id)
        ).all()
    )
    updates = list(
        db.scalars(
            select(IndustryTrendUpdate)
            .where(IndustryTrendUpdate.chain_id.in_(chain_ids))
            .order_by(desc(IndustryTrendUpdate.update_date), desc(IndustryTrendUpdate.id))
        ).all()
    )
    evidence_rows = list(
        db.scalars(
            select(IndustryChainEvidence)
            .where(IndustryChainEvidence.chain_id.in_(chain_ids))
            .order_by(desc(IndustryChainEvidence.evidence_date), desc(IndustryChainEvidence.id))
        ).all()
    )
    now = datetime.now()
    shared_xq_rows = list(
        db.scalars(
            select(XueqiuPost).where(XueqiuPost.published_at >= now - timedelta(days=21))
        ).all()
    )
    information_rows = list(
        db.scalars(
            select(InformationScreeningItem)
            .where(InformationScreeningItem.published_at >= now - timedelta(days=30))
            .order_by(desc(InformationScreeningItem.published_at), desc(InformationScreeningItem.importance), desc(InformationScreeningItem.id))
        ).all()
    )

    validations_by_chain: dict[int, list[IndustryChainTask]] = {chain_id: [] for chain_id in chain_ids}
    catalysts_by_chain: dict[int, list[IndustryTrendCatalyst]] = {chain_id: [] for chain_id in chain_ids}
    updates_by_chain: dict[int, list[IndustryTrendUpdate]] = {chain_id: [] for chain_id in chain_ids}
    evidence_by_chain: dict[int, list[IndustryChainEvidence]] = {chain_id: [] for chain_id in chain_ids}
    for row in validations:
        validations_by_chain[row.chain_id].append(row)
    for row in catalysts:
        catalysts_by_chain[row.chain_id].append(row)
    for row in updates:
        updates_by_chain[row.chain_id].append(row)
    for row in evidence_rows:
        evidence_by_chain[row.chain_id].append(row)

    expectation_snapshots = list(
        db.scalars(
            select(IndustryExpectationSnapshot)
            .where(IndustryExpectationSnapshot.chain_id.in_(chain_ids))
            .order_by(desc(IndustryExpectationSnapshot.snapshot_date), desc(IndustryExpectationSnapshot.id))
        ).all()
    )
    latest_expectation_by_chain: dict[int, IndustryExpectationSnapshot] = {}
    for row in expectation_snapshots:
        latest_expectation_by_chain.setdefault(row.chain_id, row)

    sectors: list[dict[str, Any]] = []
    for chain in chains:
        chain_validations = validations_by_chain[chain.id]
        chain_sources = evidence_by_chain[chain.id]
        chain_companies = companies_by_chain[chain.id]
        primary = companies_by_id.get(chain.primary_company_id) if chain.primary_company_id else None
        if primary is None:
            primary = next((row for row in chain_companies if row.is_primary), None)
        next_catalyst = next((row for row in catalysts_by_chain[chain.id] if row.status != "兑现"), None)
        latest_update = updates_by_chain[chain.id][0] if updates_by_chain[chain.id] else None
        dynamics = _industry_dynamics(
            db,
            chain,
            nodes_by_chain[chain.id],
            chain_companies,
            {row.id: market_snapshots.get(row.id) for row in chain_companies},
            updates_by_chain[chain.id],
            chain_validations,
            shared_xq_rows,
            information_rows,
        )
        sectors.append(
            {
                "id": chain.id,
                "name": chain.name,
                "phase": chain.phase,
                "attention_level": chain.attention_level,
                "direction_verdict": chain.direction_verdict,
                "stock_verdict": chain.stock_verdict,
                "timing_verdict": chain.timing_verdict,
                "pricing_status": chain.pricing_status,
                "primary_company": primary.name if primary else None,
                "company_recommendations": _company_recommendations_out(chain_companies),
                "sector_expectation": _industry_consensus_out(
                    chain_companies,
                    latest_expectation_by_chain.get(chain.id),
                ),
                "validation_summary": {
                    "total": len(chain_validations),
                    "confirmed": sum(row.status == "已确认" for row in chain_validations),
                    "in_progress": sum(row.status == "验证中" for row in chain_validations),
                    "failed": sum(row.status == "失败" for row in chain_validations),
                },
                "evidence_summary": {
                    "total": len(chain_sources),
                    "hard": sum(row.source_tier in {"官方硬证据", "公司披露", "行业标准"} for row in chain_sources),
                    "mutual": sum(row.verification_status == "已互证" for row in chain_sources),
                    "risk": sum((row.evidence_state or "有效") in {"存在冲突", "已失效"} for row in chain_sources),
                },
                "next_catalyst": _catalyst_out(next_catalyst) if next_catalyst else None,
                "latest_change": _update_out(latest_update) if latest_update else None,
                "next_signal": chain.next_signal,
                "invalidation": chain.invalidation,
                "action": dynamics["action"],
                "action_tone": dynamics["action_tone"],
                "action_reason": dynamics["action_reason"],
                "evidence_status": dynamics["evidence"]["status"],
                "attention": dynamics["attention"],
                "market": dynamics["market"],
                "expression": dynamics["expression"],
                "freshness": dynamics["freshness"],
            }
        )

    # 信息筛选是每日增量入口。这里建立实时派生的自动关联层：
    # 已验证材料直接计入产业证据，待验证材料只进观察层，过滤/证伪材料自动排除。
    # 不复制记录，因此信息卡后续修改或删除会立刻反映到产业趋势，避免两份数据漂移。
    matched_changes: list[dict[str, Any]] = []
    seen_information_keys: set[str] = set()
    for row in information_rows:
        if row.bucket == "filtered" or row.verification_status == "disproved":
            continue
        if row.item_key in seen_information_keys:
            continue
        associations = [
            (chain, _information_association(row, chain, nodes_by_chain[chain.id], companies_by_chain[chain.id]))
            for chain in chains
        ]
        associations = [(chain, association) for chain, association in associations if association["matched"]]
        if not associations:
            continue
        seen_information_keys.add(row.item_key)
        affected_nodes: dict[int, dict[str, Any]] = {}
        affected_companies: dict[int, dict[str, Any]] = {}
        match_reasons: list[str] = []
        for chain, association in associations:
            for node in association["nodes"]:
                affected_nodes[node.id] = {"id": node.id, "name": node.name, "node_type": node.node_type, "industry_id": chain.id}
            for company in association["companies"]:
                affected_companies[company.id] = {
                    "id": company.id,
                    "name": company.name,
                    "code": company.code,
                    "market": company.market or "A股",
                    "industry_id": chain.id,
                }
            if association["reasons"]:
                match_reasons.append(f"{chain.name.split('（', 1)[0]}：{'；'.join(association['reasons'])}")
        auto_tier, auto_action = _information_auto_tier(row)
        matched_changes.append(
            {
                "id": row.id,
                "item_key": row.item_key,
                "title": row.title,
                "summary": row.summary,
                "published_at": row.published_at,
                "source_name": row.source_name,
                "source_url": row.source_url or row.official_source_url,
                "bucket": row.bucket,
                "importance": row.importance,
                "verification_status": row.verification_status,
                "official_check_status": row.official_check_status,
                "price_status": row.price_status,
                "is_shared": len(associations) > 1,
                "affected_industries": [
                    {
                        "id": chain.id,
                        "name": chain.name,
                        "match_score": association["score"],
                        "match_method": association["method"],
                    }
                    for chain, association in associations
                ],
                "affected_nodes": list(affected_nodes.values()),
                "affected_companies": list(affected_companies.values()),
                "match_reasons": match_reasons,
                "auto_tier": auto_tier,
                "auto_action": auto_action,
            }
        )

    today_start = datetime.combine(date.today(), datetime.min.time())
    fresh_changes = [
        row for row in matched_changes
        if row["published_at"] and row["published_at"] >= today_start
    ][:8]

    recent_7d = [
        row for row in matched_changes
        if row["published_at"] and row["published_at"] >= now - timedelta(days=7)
    ]
    verified_7d = [
        row for row in recent_7d
        if row["bucket"] == "verified" and row["verification_status"] in {"verified", "cross_verified", "partial"}
    ]
    shared_7d = [row for row in recent_7d if row["is_shared"]]

    action_counts: dict[str, int] = {}
    for sector in sectors:
        action_counts[sector["action"]] = action_counts.get(sector["action"], 0) + 1
    positive = next((row for row in sectors if row["action_tone"] == "positive"), None)
    watch = next((row for row in sectors if row["action_tone"] == "watch"), None)
    negative_count = sum(row["action_tone"] == "negative" for row in sectors)
    if positive:
        overall_action, overall_tone = "出现可交易细分", "positive"
        overall_reason = f"{positive['name']}已达到“{positive['action']}”；其余方向仍按各自触发条件跟踪。"
    elif negative_count == len(sectors):
        overall_action, overall_tone = "风险收缩", "negative"
        names = "、".join(row["name"].split("（", 1)[0] for row in sectors)
        overall_reason = f"{names}当前均为风险收缩：{sectors[0]['action_reason']}"
    elif watch:
        overall_action, overall_tone = "等待触发", "watch"
        overall_reason = f"{watch['name']}已有产业变化，但资金、时点或股票表达尚未全部对齐。"
    else:
        overall_action, overall_tone = "信息不足", "neutral"
        overall_reason = "当前缺少能同时通过硬证据与盘面确认的新变量。"
    freshness_values: list[datetime] = [row["published_at"] for row in matched_changes if row["published_at"]]
    for sector in sectors:
        for value in sector["freshness"].values():
            if isinstance(value, datetime):
                freshness_values.append(value)
            elif isinstance(value, date):
                freshness_values.append(datetime.combine(value, datetime.min.time()))
    latest_at = max(freshness_values, default=None)

    merged_evidence: dict[str, dict[str, Any]] = {}
    state_priority = {"存在冲突": 4, "已失效": 3, "待复核": 2, "有效": 1}
    for row in evidence_rows:
        key = _evidence_merge_key(row)
        chain = next(item for item in chains if item.id == row.chain_id)
        current_state = _evidence_out(row)["evidence_state"]
        item = merged_evidence.get(key)
        if item is None:
            item = {
                "key": key,
                "title": row.title,
                "content": row.content,
                "source_name": row.source_name,
                "source_url": row.source_url,
                "source_tier": row.source_tier,
                "verification_status": row.verification_status,
                "evidence_state": current_state,
                "evidence_date": row.evidence_date,
                "impact_level": row.impact_level,
                "affected_industries": [],
            }
            merged_evidence[key] = item
        if not any(entry["id"] == chain.id for entry in item["affected_industries"]):
            item["affected_industries"].append({"id": chain.id, "name": chain.name})
        if row.evidence_date > item["evidence_date"]:
            item.update({"title": row.title, "content": row.content, "source_name": row.source_name, "source_url": row.source_url, "source_tier": row.source_tier, "verification_status": row.verification_status, "evidence_date": row.evidence_date, "impact_level": row.impact_level})
        if state_priority.get(current_state, 0) > state_priority.get(item["evidence_state"], 0):
            item["evidence_state"] = current_state

    for item in merged_evidence.values():
        text = str(item["title"] or "").lower()
        if any(keyword in text for keyword in AI_SHARED_EVIDENCE_KEYWORDS):
            item["association_reason"] = "共同需求"
            for chain in (row for row in chains if _is_ai_chain(row)):
                if not any(entry["id"] == chain.id for entry in item["affected_industries"]):
                    item["affected_industries"].append({"id": chain.id, "name": chain.name})

    evidence_items = sorted(
        merged_evidence.values(),
        key=lambda item: (item["evidence_date"], len(item["affected_industries"]), item["impact_level"] == "强"),
        reverse=True,
    )
    supporting = [
        item for item in evidence_items
        if item["evidence_state"] == "有效" and item["source_tier"] in {"官方硬证据", "公司披露", "行业标准"}
    ]
    risk_evidence = [item for item in evidence_items if item["evidence_state"] in {"存在冲突", "已失效", "待复核"}]
    shared_evidence = [item for item in evidence_items if len(item["affected_industries"]) > 1]

    validation_route: list[dict[str, Any]] = []
    for stage, _ in VALIDATION_ROUTE_STAGES:
        stage_items = [row for row in validations if _validation_route_stage(row) == stage]
        validation_route.append(
            {
                "stage": stage,
                "total": len(stage_items),
                "confirmed": sum(row.status == "已确认" for row in stage_items),
                "in_progress": sum(row.status == "验证中" for row in stage_items),
                "failed": sum(row.status == "失败" for row in stage_items),
                "industries": [
                    {"id": chain.id, "name": chain.name}
                    for chain in chains
                    if any(row.chain_id == chain.id for row in stage_items)
                ],
            }
        )

    risk_signals: list[dict[str, Any]] = [
        {**item, "signal_type": "反向证据"} for item in risk_evidence[:4]
    ]
    for chain in chains:
        failed_rows = [row for row in validations_by_chain[chain.id] if row.status == "失败"]
        for row in failed_rows:
            risk_signals.append({"signal_type": "验证失败", "title": row.title, "content": row.current_result or row.conclusion, "affected_industries": [{"id": chain.id, "name": chain.name}]})
        if chain.invalidation and len(risk_signals) < 6:
            risk_signals.append({"signal_type": "证伪条件", "title": chain.invalidation, "content": None, "affected_industries": [{"id": chain.id, "name": chain.name}]})

    return {
        "theme": "A股产业链",
        "overall": {
            "action": overall_action,
            "tone": overall_tone,
            "reason": overall_reason,
            "latest_at": latest_at,
            "action_counts": action_counts,
        },
        "summary": {
            "industries": len(chains),
            "sources": len(evidence_items),
            "supporting_evidence": len(supporting),
            "shared_evidence": len(shared_evidence),
            "risk_evidence": len(risk_evidence) + sum(row.status == "失败" for row in validations),
            "confirmed_validations": sum(row.status == "已确认" for row in validations),
            "total_validations": len(validations),
            "fresh_changes_7d": len(recent_7d),
            "verified_changes_7d": len(verified_7d),
            "shared_changes_7d": len(shared_7d),
        },
        "sectors": sectors,
        "fresh_changes": fresh_changes,
        "latest_changes": (evidence_items[:3] + [item for item in shared_evidence if item not in evidence_items[:3]][:3]),
        "validation_route": validation_route,
        "supporting_evidence": supporting[:5],
        "risk_signals": risk_signals[:6],
    }


@router.get("/cross-market-intelligence")
def get_cross_market_intelligence(
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Cross-market price confirmation; never overwrites formal industry conclusions."""
    try:
        return build_cross_market_intelligence(db, force_refresh=refresh)
    except Exception as exc:
        return {
            "as_of": None,
            "source_status": "unavailable",
            "principle": "跨市场行情只作为盘面确认，不替代订单、业绩、客户和产品证据；偏离只进入观察池。",
            "global_demand": {},
            "sectors": [],
            "errors": [f"跨市场行情暂不可用：{str(exc)[:180]}"],
            "method": {
                "alignment": "美股前一交易日对应A股下一交易日",
                "windows": [5, 20, 60],
                "source": "Yahoo Finance + 本地A股历史行情",
                "cache_minutes": 20,
            },
        }


@router.post("")
def create_trend(payload: TrendCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    _choice(payload.attention_level, ATTENTION_LEVELS, "关注等级")
    if db.scalar(select(IndustryChain).where(IndustryChain.name == payload.name.strip())):
        raise HTTPException(status_code=409, detail="同名产业已经存在")
    chain = IndustryChain(
        name=payload.name.strip(),
        summary=payload.summary,
        phase="观察期",
        strength=20,
        attention_level=payload.attention_level,
        direction_verdict="观察",
        stock_verdict="观察",
        timing_verdict="观察",
        overall_verdict="观察",
        pricing_status="部分定价",
        phase_entered_at=date.today(),
        status="paused" if payload.attention_level == "暂停" else "active",
    )
    db.add(chain)
    db.flush()
    _save_version(db, chain)
    db.commit()
    return _detail_out(db, chain)


@router.get("/{chain_id}")
def get_trend(chain_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _detail_out(db, _chain_or_404(db, chain_id))


def _research_setting_out(row: IndustryTrendResearchSetting | None, chain_id: int) -> dict[str, Any]:
    return {
        "id": row.id if row else None,
        "chain_id": chain_id,
        "update_mode": "全面更新",
        "priority_nodes": _json_load(row.priority_nodes_json, []) if row else [],
        "priority_companies": _json_load(row.priority_companies_json, []) if row else [],
        "source_preferences": _json_load(row.source_preferences_json, list(DEFAULT_SOURCE_PREFERENCES)) if row else list(DEFAULT_SOURCE_PREFERENCES),
        "excluded_keywords": _json_load(row.excluded_keywords_json, []) if row else [],
        "evidence_rules": row.evidence_rules if row else DEFAULT_EVIDENCE_RULES,
        "custom_instructions": row.custom_instructions if row else None,
        "token_budget": row.token_budget if row else 24000,
        "allow_new_nodes": row.allow_new_nodes if row else True,
        "allow_new_companies": row.allow_new_companies if row else True,
        "draft_only": True,
        "last_researched_at": row.last_researched_at if row else None,
        "created_at": row.created_at if row else None,
        "updated_at": row.updated_at if row else None,
    }


def _research_setting(db: Session, chain_id: int, create: bool = False) -> IndustryTrendResearchSetting | None:
    row = db.scalar(select(IndustryTrendResearchSetting).where(IndustryTrendResearchSetting.chain_id == chain_id))
    if row is None and create:
        row = IndustryTrendResearchSetting(
            chain_id=chain_id,
            source_preferences_json=_json_dump(DEFAULT_SOURCE_PREFERENCES),
            evidence_rules=DEFAULT_EVIDENCE_RULES,
            draft_only=True,
        )
        db.add(row)
        db.flush()
    return row


@router.get("/{chain_id}/research-settings")
def get_research_settings(chain_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    _chain_or_404(db, chain_id)
    return _research_setting_out(_research_setting(db, chain_id), chain_id)


@router.put("/{chain_id}/research-settings")
def save_research_settings(chain_id: int, payload: ResearchSettingPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    _chain_or_404(db, chain_id)
    row = _research_setting(db, chain_id, create=True)
    assert row is not None
    row.priority_nodes_json = _json_dump(list(dict.fromkeys(item.strip() for item in payload.priority_nodes if item.strip())))
    row.priority_companies_json = _json_dump(list(dict.fromkeys(item.strip() for item in payload.priority_companies if item.strip())))
    row.source_preferences_json = _json_dump(list(dict.fromkeys(item.strip() for item in payload.source_preferences if item.strip())))
    row.excluded_keywords_json = _json_dump(list(dict.fromkeys(item.strip() for item in payload.excluded_keywords if item.strip())))
    row.evidence_rules = payload.evidence_rules
    row.custom_instructions = payload.custom_instructions
    row.token_budget = payload.token_budget
    row.allow_new_nodes = payload.allow_new_nodes
    row.allow_new_companies = payload.allow_new_companies
    row.draft_only = True
    row.updated_at = now_utc()
    db.commit()
    return _research_setting_out(row, chain_id)


def _material_key(chain_id: int, payload: MaterialCreate) -> str:
    identity = (payload.source_url or "").strip() or f"{payload.title.strip()}|{payload.material_date.isoformat()}|{payload.source_name or ''}"
    return hashlib.sha1(f"{chain_id}|{identity}".encode("utf-8")).hexdigest()


@router.post("/{chain_id}/materials")
def create_material(chain_id: int, payload: MaterialCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    _chain_or_404(db, chain_id)
    _choice(payload.change_type, MATERIAL_CHANGE_TYPES, "材料变化类型")
    key = _material_key(chain_id, payload)
    existing = db.scalar(
        select(IndustryTrendMaterial).where(
            IndustryTrendMaterial.chain_id == chain_id,
            IndustryTrendMaterial.material_key == key,
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="相同材料已经在更新队列中")
    row = IndustryTrendMaterial(
        chain_id=chain_id,
        material_key=key,
        title=payload.title.strip(),
        content=payload.content,
        source_type=payload.source_type.strip() or "本地对话",
        source_name=payload.source_name,
        source_url=payload.source_url,
        material_date=payload.material_date,
        change_type=payload.change_type,
        status="待处理",
        note=payload.note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _material_out(row)


@router.patch("/{chain_id}/materials/{material_id}")
def update_material(chain_id: int, material_id: int, payload: MaterialPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    _chain_or_404(db, chain_id)
    row = db.scalar(
        select(IndustryTrendMaterial).where(
            IndustryTrendMaterial.id == material_id,
            IndustryTrendMaterial.chain_id == chain_id,
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="更新材料不存在")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("status"):
        _choice(changes["status"], MATERIAL_STATUSES, "材料状态")
    if changes.get("change_type"):
        _choice(changes["change_type"], MATERIAL_CHANGE_TYPES, "材料变化类型")
    for field, value in changes.items():
        setattr(row, field, value)
    if row.status in {"已纳入", "忽略"}:
        row.processed_at = now_utc()
    elif row.status == "待处理":
        row.processed_at = None
    db.commit()
    return _material_out(row)


@router.delete("/{chain_id}/materials/{material_id}")
def delete_material(chain_id: int, material_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    _chain_or_404(db, chain_id)
    row = db.scalar(
        select(IndustryTrendMaterial).where(
            IndustryTrendMaterial.id == material_id,
            IndustryTrendMaterial.chain_id == chain_id,
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="更新材料不存在")
    db.delete(row)
    db.commit()
    return {"status": "ok"}


@router.patch("/{chain_id}/sources/{evidence_id}")
def update_evidence_state(chain_id: int, evidence_id: int, payload: EvidenceStatePatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    _chain_or_404(db, chain_id)
    _choice(payload.evidence_state, EVIDENCE_STATES, "证据状态")
    row = db.scalar(
        select(IndustryChainEvidence).where(
            IndustryChainEvidence.id == evidence_id,
            IndustryChainEvidence.chain_id == chain_id,
        )
    )
    if not row:
        raise HTTPException(status_code=404, detail="证据不存在")
    row.evidence_state = payload.evidence_state
    row.valid_until = payload.valid_until
    row.conflict_note = payload.conflict_note
    db.commit()
    node_ids = list(
        db.scalars(
            select(IndustryTrendNodeSource.node_id).where(IndustryTrendNodeSource.evidence_id == row.id)
        )
    )
    return _evidence_out(row, node_ids)


@router.post("/{chain_id}/decisions")
def create_industry_decision(chain_id: int, payload: IndustryDecisionCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.decision_review import decision_to_out, normalize_stock_code, refresh_decision_performance

    chain = _chain_or_404(db, chain_id)
    company = None
    if payload.primary_company_id:
        company = db.scalar(
            select(IndustryChainCompany).where(
                IndustryChainCompany.id == payload.primary_company_id,
                IndustryChainCompany.chain_id == chain_id,
            )
        )
    if company is None and chain.primary_company_id:
        company = db.get(IndustryChainCompany, chain.primary_company_id)
    if payload.decision_code in {"A", "B", "C"}:
        if not company or (company.market or "A股") != "A股" or not company.code:
            raise HTTPException(status_code=422, detail="A/B/C决策需要选择产业内A股主选公司")
    company_code = (company.full_code or company.code) if company else None
    code, full_code = normalize_stock_code(company_code)
    if company and company.code and not full_code:
        raise HTTPException(status_code=422, detail="主选股票代码无法识别")
    signal_at = now_utc()
    decision_key = "industry-" + hashlib.sha1(
        f"{chain.id}|{chain.revision}|{signal_at.isoformat()}".encode("utf-8")
    ).hexdigest()[:24]
    alternatives = [
        f"{item.name} {item.full_code or item.code or ''}".strip()
        for item in db.scalars(
            select(IndustryChainCompany)
            .where(IndustryChainCompany.chain_id == chain.id, IndustryChainCompany.market == "A股")
            .order_by(IndustryChainCompany.sort_order, IndustryChainCompany.id)
        )
        if not company or item.id != company.id
    ][:20]
    pricing_verdict = (payload.pricing_verdict or "").strip() or "；".join(
        item for item in [chain.pricing_status, chain.priced_in, chain.not_priced_in] if item
    )
    thesis = (payload.thesis or "").strip() or chain.summary or chain.investment_logic or chain.name
    triggers = [item.strip() for item in payload.trigger_conditions if item.strip()]
    invalidations = [item.strip() for item in payload.invalidation_conditions if item.strip()]
    if not triggers and chain.next_signal:
        triggers = [chain.next_signal]
    if not invalidations and chain.invalidation:
        invalidations = [chain.invalidation]
    industry_snapshot = _snapshot(db, chain)
    selected_company = next(
        (
            item
            for item in industry_snapshot.get("companies", [])
            if company
            and (
                item.get("id") == company.id
                or (company.full_code and item.get("full_code") == company.full_code)
            )
        ),
        None,
    )
    policy_evaluation = evaluate_industry_policies(
        industry_snapshot,
        selected_company,
        why_best=(payload.why_best or "").strip() or (company.primary_reason if company else "") or "",
    )
    snapshot = {
        "industry": industry_snapshot,
        "policy_evaluation": policy_evaluation,
        "decision_inputs": payload.model_dump(mode="json"),
        "frozen_at": signal_at,
        "rule": "从决策形成后的下一交易日开盘模拟成交；产业研究和证据快照不可修改",
    }
    row = IndustryTrendDecision(
        decision_key=decision_key,
        chain_id=chain.id,
        chain_name=chain.name,
        snapshot_json=_json_dump(snapshot),
        signal_at=signal_at,
        decision_code=payload.decision_code,
        primary_stock_code=code,
        primary_stock_name=company.name if company else None,
        primary_full_code=full_code,
        alternatives_json=_json_dump(alternatives),
        direction_verdict=chain.direction_verdict,
        stock_verdict=chain.stock_verdict,
        timing_verdict=chain.timing_verdict,
        pricing_verdict=pricing_verdict,
        thesis=thesis,
        why_best=(payload.why_best or "").strip() or (company.primary_reason if company else "") or "",
        trigger_conditions_json=_json_dump(triggers),
        invalidation_conditions_json=_json_dump(invalidations),
        planned_horizon=payload.planned_horizon,
        cost_bps=payload.cost_bps,
        execution_status="shadow_waiting" if payload.decision_code != "A" else "waiting_entry",
        performance_json="{}",
    )
    db.add(row)
    db.flush()
    refresh_decision_performance(db, row)
    db.commit()
    db.refresh(row)
    return {"status": "created", "item": decision_to_out(row)}


@router.patch("/{chain_id}")
def update_trend(chain_id: int, payload: TrendPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, options, label in (
        ("phase", PHASES, "产业阶段"),
        ("attention_level", ATTENTION_LEVELS, "关注等级"),
        ("direction_verdict", VERDICTS, "方向判断"),
        ("stock_verdict", VERDICTS, "股票判断"),
        ("timing_verdict", VERDICTS, "时点判断"),
        ("pricing_status", PRICING_STATUSES, "定价状态"),
    ):
        if field in changes and changes[field] is not None:
            _choice(changes[field], options, label)
    old_phase = chain.phase
    for field, value in changes.items():
        if field == "drivers":
            chain.drivers_json = _json_dump(list(dict.fromkeys(item.strip() for item in value if item.strip())))
        elif hasattr(chain, field):
            setattr(chain, field, value)
    chain.status = "paused" if chain.attention_level == "暂停" else "active"
    if chain.phase != old_phase:
        chain.phase_entered_at = date.today()
        update = IndustryTrendUpdate(
            chain_id=chain.id,
            update_date=date.today(),
            content=f"产业阶段由{old_phase}调整为{chain.phase}",
            impact="阶段变化已由用户确认。",
            affects_phase=True,
            phase_suggestion=chain.phase,
        )
        db.add(update)
        chain.last_change_at = now_utc()
    _save_version(db, chain)
    db.commit()
    return _detail_out(db, chain)


@router.post("/{chain_id}/nodes")
def create_node(chain_id: int, payload: NodePayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    _choice(payload.node_type, NODE_TYPES, "节点类型")
    if payload.maturity_status:
        _choice(payload.maturity_status, MATURITY_STATUSES, "成熟状态")
    for value, label in ((payload.profit_elasticity, "利润弹性"), (payload.localization, "国产替代程度"), (payload.investment_importance, "投资重要性")):
        if value:
            _choice(value, LEVELS, label)
    row = IndustryTrendNode(chain_id=chain_id, **payload.model_dump())
    db.add(row)
    db.flush()
    _save_version(db, chain)
    db.commit()
    return _node_out(row)


@router.patch("/{chain_id}/nodes/{node_id}")
def update_node(chain_id: int, node_id: int, payload: NodePatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    row = _node_or_404(db, chain_id, node_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("node_type"):
        _choice(changes["node_type"], NODE_TYPES, "节点类型")
    if changes.get("maturity_status"):
        _choice(changes["maturity_status"], MATURITY_STATUSES, "成熟状态")
    for field in ("profit_elasticity", "localization", "investment_importance"):
        if changes.get(field):
            _choice(changes[field], LEVELS, field)
    for field, value in changes.items():
        setattr(row, field, value)
    _save_version(db, chain)
    db.commit()
    return _node_out(row)


@router.delete("/{chain_id}/nodes/{node_id}")
def delete_node(chain_id: int, node_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    chain = _chain_or_404(db, chain_id)
    row = _node_or_404(db, chain_id, node_id)
    for company in db.scalars(select(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain_id)).all():
        company.node_ids_json = _json_dump([value for value in _json_load(company.node_ids_json, []) if value != node_id])
    db.execute(
        delete(IndustryTrendEdge).where(
            IndustryTrendEdge.chain_id == chain_id,
            or_(IndustryTrendEdge.from_node_id == node_id, IndustryTrendEdge.to_node_id == node_id),
        )
    )
    db.execute(
        delete(IndustryChainTask).where(
            IndustryChainTask.chain_id == chain_id,
            IndustryChainTask.node_id == node_id,
        )
    )
    db.delete(row)
    db.flush()
    _save_version(db, chain)
    db.commit()
    return {"status": "ok", "message": "产业节点已删除"}


@router.post("/{chain_id}/edges")
def create_edge(chain_id: int, payload: EdgePayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    _node_or_404(db, chain_id, payload.from_node_id)
    _node_or_404(db, chain_id, payload.to_node_id)
    if db.scalar(select(IndustryTrendEdge).where(IndustryTrendEdge.chain_id == chain_id, IndustryTrendEdge.from_node_id == payload.from_node_id, IndustryTrendEdge.to_node_id == payload.to_node_id)):
        raise HTTPException(status_code=409, detail="该节点连线已经存在")
    row = IndustryTrendEdge(chain_id=chain_id, **payload.model_dump())
    db.add(row)
    db.flush()
    _save_version(db, chain)
    db.commit()
    return _edge_out(row)


@router.delete("/{chain_id}/edges/{edge_id}")
def delete_edge(chain_id: int, edge_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    chain = _chain_or_404(db, chain_id)
    row = db.scalar(select(IndustryTrendEdge).where(IndustryTrendEdge.id == edge_id, IndustryTrendEdge.chain_id == chain_id))
    if not row:
        raise HTTPException(status_code=404, detail="节点连线不存在")
    db.delete(row)
    db.flush()
    _save_version(db, chain)
    db.commit()
    return {"status": "ok", "message": "节点连线已删除"}


def _validate_company_payload(payload: CompanyPayload | CompanyPatch) -> None:
    data = payload.model_dump(exclude_unset=True)
    if data.get("market"):
        _choice(data["market"], MARKETS, "市场")
    for field, options, label in (
        ("tracking_status", TRACKING_STATUSES, "关注状态"),
        ("verification_status", VERIFICATION_STATUSES, "验证状态"),
        ("pricing_status", PRICING_STATUSES, "定价状态"),
        ("expectation_gap_status", EXPECTATION_GAP_STATUSES, "预期差状态"),
    ):
        if data.get(field):
            _choice(data[field], options, label)


def _validated_node_ids(db: Session, chain_id: int, node_ids: list[int]) -> list[int]:
    if not node_ids:
        return []
    valid = set(
        db.scalars(
            select(IndustryTrendNode.id).where(
                IndustryTrendNode.chain_id == chain_id,
                IndustryTrendNode.id.in_(node_ids),
            )
        ).all()
    )
    return [node_id for node_id in dict.fromkeys(node_ids) if node_id in valid]


def _company_identity(db: Session, code: str, name: str, market: str, exchange: str | None) -> dict[str, str]:
    market = _choice(market, MARKETS, "市场")
    if market == "A股":
        try:
            stock = ensure_stock(db, code, name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "code": stock.code,
            "name": stock.name,
            "exchange": stock.exchange,
            "full_code": stock.full_code,
            "market": "A股",
        }
    clean_code = code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,32}", clean_code):
        raise HTTPException(status_code=400, detail="海外公司代码只允许字母、数字、点和横线")
    prefix = MARKET_PREFIXES[market]
    return {
        "code": clean_code,
        "name": name.strip(),
        "exchange": (exchange or MARKET_DEFAULT_EXCHANGES[market]).strip().upper(),
        "full_code": f"{prefix}:{clean_code}",
        "market": market,
    }


def _company_lookup_keys(code: str | None, full_code: str | None) -> list[str]:
    return [value.strip().upper() for value in (code, full_code) if value and value.strip()]


def _set_primary(db: Session, chain: IndustryChain, row: IndustryChainCompany, enabled: bool) -> None:
    if enabled:
        if (row.market or "A股") != "A股":
            raise HTTPException(status_code=400, detail="A股交易首选仅用于A股投资表达；海外公司请作为全球产业坐标")
        for other in db.scalars(select(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain.id, IndustryChainCompany.id != row.id)).all():
            other.is_primary = False
        row.is_primary = True
        chain.primary_company_id = row.id
    elif chain.primary_company_id == row.id:
        row.is_primary = False
        chain.primary_company_id = None


@router.post("/{chain_id}/companies")
def create_company(chain_id: int, payload: CompanyPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    _validate_company_payload(payload)
    identity = _company_identity(db, payload.code, payload.name, payload.market, payload.exchange)
    if payload.is_domestic_alternative and identity["market"] != "A股":
        raise HTTPException(status_code=400, detail="国内可替代公司当前只用于A股候选")
    if db.scalar(select(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain_id, IndustryChainCompany.full_code == identity["full_code"])):
        raise HTTPException(status_code=409, detail="该公司已在全球公司池中")
    node_ids = _validated_node_ids(db, chain_id, payload.node_ids)
    row = IndustryChainCompany(
        chain_id=chain_id,
        code=identity["code"],
        name=identity["name"],
        exchange=identity["exchange"],
        full_code=identity["full_code"],
        market=identity["market"],
        external_url=payload.external_url,
        position=payload.position,
        company_standing=payload.company_standing,
        core_logic=payload.core_advantage,
        benefit_directness=payload.benefit_directness,
        profit_path=payload.profit_path,
        verification_status=payload.verification_status,
        tracking_status=payload.tracking_status,
        pricing_status=payload.pricing_status,
        is_global_leader=payload.is_global_leader,
        is_domestic_alternative=payload.is_domestic_alternative,
        primary_reason=payload.primary_reason,
        node_ids_json=_json_dump(node_ids),
        main_risk=payload.main_risk,
        market_implied_expectation=payload.market_implied_expectation,
        evidence_based_expectation=payload.evidence_based_expectation,
        expectation_gap_status=payload.expectation_gap_status,
        expectation_gap_reason=payload.expectation_gap_reason,
        expectation_trigger=payload.expectation_trigger,
        expectation_invalidation=payload.expectation_invalidation,
        expectation_as_of=payload.expectation_as_of,
        expectation_anchor_market_cap=payload.expectation_anchor_market_cap,
        expectation_evidence_growth_pct=payload.expectation_evidence_growth_pct,
        expectation_evidence_acceleration_pct=payload.expectation_evidence_acceleration_pct,
        sort_order=payload.sort_order,
    )
    db.add(row)
    db.flush()
    _set_primary(db, chain, row, payload.is_primary)
    _save_version(db, chain)
    db.commit()
    return _company_out(row)


@router.patch("/{chain_id}/companies/{company_id}")
def update_company(chain_id: int, company_id: int, payload: CompanyPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    row = _company_or_404(db, chain_id, company_id)
    _validate_company_payload(payload)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("is_domestic_alternative") and (row.market or "A股") != "A股":
        raise HTTPException(status_code=400, detail="国内可替代公司当前只用于A股候选")
    is_primary = changes.pop("is_primary", None)
    for field, value in changes.items():
        if field == "core_advantage":
            row.core_logic = value
        elif field == "node_ids":
            row.node_ids_json = _json_dump(_validated_node_ids(db, chain_id, value))
        else:
            setattr(row, field, value)
    if is_primary is not None:
        _set_primary(db, chain, row, is_primary)
    _save_version(db, chain)
    db.commit()
    return _company_out(row)


@router.delete("/{chain_id}/companies/{company_id}")
def delete_company(chain_id: int, company_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    chain = _chain_or_404(db, chain_id)
    row = _company_or_404(db, chain_id, company_id)
    if chain.primary_company_id == row.id:
        chain.primary_company_id = None
    db.delete(row)
    db.flush()
    _save_version(db, chain)
    db.commit()
    return {"status": "ok", "message": "产业股票已删除"}


def _validate_validation(data: dict[str, Any]) -> None:
    if data.get("status"):
        _choice(data["status"], VERIFICATION_STATUSES, "验证状态")


def _validate_validation_links(db: Session, chain_id: int, data: dict[str, Any]) -> None:
    if data.get("company_id") is not None:
        _company_or_404(db, chain_id, data["company_id"])
    if data.get("node_id") is not None:
        _node_or_404(db, chain_id, data["node_id"])


def _add_failed_validation_suggestion(db: Session, chain: IndustryChain, title: str) -> None:
    db.add(
        IndustryTrendUpdate(
            chain_id=chain.id,
            update_date=date.today(),
            content=f"验证指标失败：{title}",
            impact="该失败项可能削弱产业方向，等待用户确认是否降为观察或否决。",
            next_verification="复核失败原因，并更新方向判断或证伪条件。",
            affects_decision=True,
            decision_suggestion_json=_json_dump({"direction_verdict": "观察或否决"}),
        )
    )
    chain.last_change_at = now_utc()


@router.post("/{chain_id}/validations")
def create_validation(chain_id: int, payload: ValidationPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    data = payload.model_dump()
    _validate_validation(data)
    _validate_validation_links(db, chain_id, data)
    row = IndustryChainTask(
        chain_id=chain_id,
        company_id=data["company_id"],
        node_id=data["node_id"],
        title=data["name"],
        description=data["criteria"],
        criteria=data["criteria"],
        current_result=data["current_result"],
        status=data["status"],
        due_date=data["target_date"],
        source_name=data["source_name"],
        source_url=data["source_url"],
        priority=data["priority"],
        sort_order=data["sort_order"],
    )
    db.add(row)
    if row.status == "失败":
        _add_failed_validation_suggestion(db, chain, row.title)
    db.flush()
    _save_version(db, chain)
    db.commit()
    return _validation_out(row)


@router.patch("/{chain_id}/validations/{validation_id}")
def update_validation(chain_id: int, validation_id: int, payload: ValidationPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    row = _task_or_404(db, chain_id, validation_id)
    old_status = row.status
    changes = payload.model_dump(exclude_unset=True)
    _validate_validation(changes)
    _validate_validation_links(db, chain_id, changes)
    mapping = {"name": "title", "target_date": "due_date"}
    for field, value in changes.items():
        setattr(row, mapping.get(field, field), value)
        if field == "criteria":
            row.description = value
        if field == "current_result":
            row.conclusion = value
    if row.status == "失败" and old_status != "失败":
        _add_failed_validation_suggestion(db, chain, row.title)
    _save_version(db, chain)
    db.commit()
    return _validation_out(row)


@router.delete("/{chain_id}/validations/{validation_id}")
def delete_validation(chain_id: int, validation_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    chain = _chain_or_404(db, chain_id)
    db.delete(_task_or_404(db, chain_id, validation_id))
    db.flush()
    _save_version(db, chain)
    db.commit()
    return {"status": "ok", "message": "验证指标已删除"}


def _validate_catalyst(db: Session, chain_id: int, data: dict[str, Any]) -> None:
    if data.get("status"):
        _choice(data["status"], CATALYST_STATUSES, "催化事件状态")
    if data.get("importance"):
        _choice(data["importance"], CATALYST_IMPORTANCE_LEVELS, "催化事件重要程度")
    if data.get("impact_node_id") is not None:
        _node_or_404(db, chain_id, data["impact_node_id"])
    if data.get("impact_company_id") is not None:
        _company_or_404(db, chain_id, data["impact_company_id"])


@router.post("/{chain_id}/catalysts")
def create_catalyst(chain_id: int, payload: CatalystPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    data = payload.model_dump()
    _validate_catalyst(db, chain_id, data)
    row = IndustryTrendCatalyst(chain_id=chain_id, **data)
    db.add(row)
    chain.last_change_at = now_utc()
    db.flush()
    _save_version(db, chain)
    db.commit()
    return _catalyst_out(row)


@router.patch("/{chain_id}/catalysts/{catalyst_id}")
def update_catalyst(chain_id: int, catalyst_id: int, payload: CatalystPatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    row = _catalyst_or_404(db, chain_id, catalyst_id)
    changes = payload.model_dump(exclude_unset=True)
    _validate_catalyst(db, chain_id, changes)
    for field, value in changes.items():
        setattr(row, field, value)
    chain.last_change_at = now_utc()
    _save_version(db, chain)
    db.commit()
    return _catalyst_out(row)


@router.delete("/{chain_id}/catalysts/{catalyst_id}")
def delete_catalyst(chain_id: int, catalyst_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    chain = _chain_or_404(db, chain_id)
    db.delete(_catalyst_or_404(db, chain_id, catalyst_id))
    db.flush()
    _save_version(db, chain)
    db.commit()
    return {"status": "ok", "message": "关键催化事件已删除"}


@router.post("/{chain_id}/updates")
def create_update(chain_id: int, payload: UpdatePayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    if payload.phase_suggestion:
        _choice(payload.phase_suggestion, PHASES, "阶段建议")
    if payload.causal_stage:
        _choice(payload.causal_stage, CAUSAL_STAGES, "因果阶段")
    if payload.evidence_type:
        _choice(payload.evidence_type, CAUSAL_EVIDENCE_TYPES, "证据类型")
    if payload.signal_status:
        _choice(payload.signal_status, CAUSAL_SIGNAL_STATUSES, "信号状态")
    if payload.sell_pressure:
        _choice(payload.sell_pressure, SELL_PRESSURE_LEVELS, "卖压等级")
    row = IndustryTrendUpdate(
        chain_id=chain_id,
        **payload.model_dump(exclude={"decision_suggestion"}),
        decision_suggestion_json=_json_dump(payload.decision_suggestion),
    )
    db.add(row)
    chain.last_change_at = now_utc()
    db.flush()
    _save_version(db, chain)
    db.commit()
    return _update_out(row)


@router.patch("/{chain_id}/updates/{update_id}")
def update_update(chain_id: int, update_id: int, payload: UpdatePatch, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    row = _update_or_404(db, chain_id, update_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("phase_suggestion"):
        _choice(changes["phase_suggestion"], PHASES, "阶段建议")
    for field, options, label in (
        ("causal_stage", CAUSAL_STAGES, "因果阶段"),
        ("evidence_type", CAUSAL_EVIDENCE_TYPES, "证据类型"),
        ("signal_status", CAUSAL_SIGNAL_STATUSES, "信号状态"),
        ("sell_pressure", SELL_PRESSURE_LEVELS, "卖压等级"),
    ):
        if changes.get(field):
            _choice(changes[field], options, label)
    for field, value in changes.items():
        if field == "decision_suggestion":
            row.decision_suggestion_json = _json_dump(value)
        else:
            setattr(row, field, value)
    chain.last_change_at = now_utc()
    _save_version(db, chain)
    db.commit()
    return _update_out(row)


@router.delete("/{chain_id}/updates/{update_id}")
def delete_update(chain_id: int, update_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    chain = _chain_or_404(db, chain_id)
    db.delete(_update_or_404(db, chain_id, update_id))
    db.flush()
    _save_version(db, chain)
    db.commit()
    return {"status": "ok", "message": "跟踪记录已删除"}


def _upsert_draft(db: Session, chain_id: int, draft_type: str, source: str, payload: dict[str, Any]) -> IndustryTrendDraft:
    row = db.scalar(select(IndustryTrendDraft).where(IndustryTrendDraft.chain_id == chain_id))
    if not row:
        row = IndustryTrendDraft(chain_id=chain_id)
        db.add(row)
    row.draft_type = draft_type
    row.source = source
    row.payload_json = _json_dump(payload)
    row.updated_at = now_utc()
    db.flush()
    return row


def _apply_update_delta(
    db: Session,
    chain: IndustryChain,
    result: CodexUpdateDelta,
    setting: IndustryTrendResearchSetting | None,
) -> None:
    allow_new_nodes = setting.allow_new_nodes if setting else True
    allow_new_companies = setting.allow_new_companies if setting else True
    changed = False

    core_changes = result.core_patch.model_dump(exclude_none=True)
    for field, options, label in (
        ("phase", PHASES, "产业阶段"),
        ("attention_level", ATTENTION_LEVELS, "关注等级"),
        ("direction_verdict", VERDICTS, "方向判断"),
        ("stock_verdict", VERDICTS, "股票判断"),
        ("timing_verdict", VERDICTS, "时点判断"),
        ("pricing_status", PRICING_STATUSES, "定价状态"),
    ):
        if core_changes.get(field) is not None:
            _choice(core_changes[field], options, label)
    old_phase = chain.phase
    for field, value in core_changes.items():
        if field == "drivers":
            chain.drivers_json = _json_dump(list(dict.fromkeys(item.strip() for item in value if item.strip())))
        elif hasattr(chain, field):
            setattr(chain, field, value)
        changed = True
    if chain.phase != old_phase:
        chain.phase_entered_at = date.today()
    chain.status = "paused" if chain.attention_level == "暂停" else "active"

    nodes = list(db.scalars(select(IndustryTrendNode).where(IndustryTrendNode.chain_id == chain.id).order_by(IndustryTrendNode.sort_order, IndustryTrendNode.id)))
    node_by_name = {row.name: row for row in nodes}
    for item in result.nodes:
        row = node_by_name.get(item.name)
        if row is None:
            if not allow_new_nodes or item.action != "新增":
                continue
            node_type = item.node_type if item.node_type in NODE_TYPES else "光互联产品"
            row = IndustryTrendNode(chain_id=chain.id, name=item.name, node_type=node_type, sort_order=(len(node_by_name) + 1) * 10)
            db.add(row)
            db.flush()
            node_by_name[row.name] = row
            changed = True
        node_changes = item.model_dump(exclude={"action", "name"}, exclude_none=True)
        if "node_type" in node_changes:
            _choice(node_changes["node_type"], NODE_TYPES, "节点类型")
        if "maturity_status" in node_changes:
            _choice(node_changes["maturity_status"], MATURITY_STATUSES, "成熟状态")
        for field in ("profit_elasticity", "localization", "investment_importance"):
            if field in node_changes:
                _choice(node_changes[field], LEVELS, field)
        for field, value in node_changes.items():
            setattr(row, field, value)
            changed = True

    existing_edges = {
        (row.from_node_id, row.to_node_id)
        for row in db.scalars(select(IndustryTrendEdge).where(IndustryTrendEdge.chain_id == chain.id)).all()
    }
    for edge in result.edges_add:
        source = node_by_name.get(edge.from_name)
        target = node_by_name.get(edge.to_name)
        if not source or not target or source.id == target.id or (source.id, target.id) in existing_edges:
            continue
        db.add(IndustryTrendEdge(chain_id=chain.id, from_node_id=source.id, to_node_id=target.id))
        existing_edges.add((source.id, target.id))
        changed = True

    companies = list(db.scalars(select(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain.id).order_by(IndustryChainCompany.sort_order, IndustryChainCompany.id)))
    company_by_name = {row.name: row for row in companies}
    company_by_key: dict[str, IndustryChainCompany] = {}
    for row in companies:
        for key in _company_lookup_keys(row.code, row.full_code):
            company_by_key[key] = row
    for item in result.companies:
        row = None
        for key in _company_lookup_keys(item.code, item.code):
            row = company_by_key.get(key)
            if row:
                break
        row = row or company_by_name.get(item.name)
        if row is None:
            if not allow_new_companies or item.action != "新增" or not item.code:
                continue
            try:
                identity = _company_identity(db, item.code, item.name, item.market or "A股", item.exchange)
            except HTTPException:
                continue
            row = IndustryChainCompany(
                chain_id=chain.id,
                code=identity["code"],
                name=identity["name"],
                exchange=identity["exchange"],
                full_code=identity["full_code"],
                market=identity["market"],
                tracking_status="观察",
                verification_status="未验证",
                pricing_status="部分定价",
                node_ids_json="[]",
                sort_order=(len(company_by_name) + 1) * 10,
            )
            db.add(row)
            db.flush()
            company_by_name[row.name] = row
            for key in _company_lookup_keys(row.code, row.full_code):
                company_by_key[key] = row
            changed = True
        company_changes = item.model_dump(exclude={"action", "name", "code", "market", "exchange", "node_names", "is_primary"}, exclude_none=True)
        for source_field, target_field in (("core_advantage", "core_logic"),):
            if source_field in company_changes:
                company_changes[target_field] = company_changes.pop(source_field)
        for field in ("tracking_status", "verification_status", "pricing_status"):
            if field not in company_changes:
                continue
            options = TRACKING_STATUSES if field == "tracking_status" else VERIFICATION_STATUSES if field == "verification_status" else PRICING_STATUSES
            _choice(company_changes[field], options, field)
        if company_changes.get("expectation_gap_status"):
            _choice(company_changes["expectation_gap_status"], EXPECTATION_GAP_STATUSES, "预期差状态")
        for field, value in company_changes.items():
            if field == "is_domestic_alternative" and value and (row.market or "A股") != "A股":
                value = False
            setattr(row, field, value)
            changed = True
        if item.expectation_gap_status and item.expectation_gap_status != "无法判断":
            row.expectation_as_of = date.today()
        if item.node_names:
            current_ids = [value for value in _json_load(row.node_ids_json, []) if isinstance(value, int)]
            for name in item.node_names:
                node = node_by_name.get(name)
                if node and node.id not in current_ids:
                    current_ids.append(node.id)
                    changed = True
            row.node_ids_json = _json_dump(current_ids)
        if item.is_primary is True and (row.market or "A股") == "A股":
            _set_primary(db, chain, row, True)
            changed = True

    validations = list(db.scalars(select(IndustryChainTask).where(IndustryChainTask.chain_id == chain.id).order_by(IndustryChainTask.sort_order, IndustryChainTask.id)))
    validation_by_name = {row.title: row for row in validations}
    for item in result.validations:
        row = validation_by_name.get(item.name)
        if row is None:
            if item.action != "新增":
                continue
            row = IndustryChainTask(chain_id=chain.id, title=item.name, status="未验证", priority="中", sort_order=(len(validation_by_name) + 1) * 10)
            db.add(row)
            db.flush()
            validation_by_name[row.title] = row
            changed = True
        validation_changes = item.model_dump(exclude={"action", "name", "company_code", "node_name"}, exclude_none=True)
        if "criteria" in validation_changes:
            validation_changes["description"] = validation_changes["criteria"]
        if "target_date" in validation_changes:
            validation_changes["due_date"] = validation_changes.pop("target_date")
        if "status" in validation_changes:
            _choice(validation_changes["status"], VERIFICATION_STATUSES, "验证状态")
        for field, value in validation_changes.items():
            setattr(row, field, value)
            changed = True
        if item.node_name and item.node_name in node_by_name:
            row.node_id = node_by_name[item.node_name].id
        if item.company_code:
            company = company_by_key.get(item.company_code.strip().upper())
            if company:
                row.company_id = company.id

    catalysts = list(db.scalars(select(IndustryTrendCatalyst).where(IndustryTrendCatalyst.chain_id == chain.id).order_by(IndustryTrendCatalyst.sort_order, IndustryTrendCatalyst.id)))
    catalyst_by_name = {row.event_name: row for row in catalysts}
    for item in result.catalysts:
        row = catalyst_by_name.get(item.event_name)
        if row is None:
            if item.action != "新增":
                continue
            row = IndustryTrendCatalyst(chain_id=chain.id, event_name=item.event_name, event_type="其他", importance="中", status="预期", sort_order=(len(catalyst_by_name) + 1) * 10)
            db.add(row)
            db.flush()
            catalyst_by_name[row.event_name] = row
            changed = True
        catalyst_changes = item.model_dump(exclude={"action", "event_name", "impact_node_name", "impact_company_code"}, exclude_none=True)
        if "importance" in catalyst_changes:
            _choice(catalyst_changes["importance"], CATALYST_IMPORTANCE_LEVELS, "事件重要性")
        if "status" in catalyst_changes:
            _choice(catalyst_changes["status"], CATALYST_STATUSES, "事件状态")
        for field, value in catalyst_changes.items():
            setattr(row, field, value)
            changed = True
        if item.impact_node_name and item.impact_node_name in node_by_name:
            row.impact_node_id = node_by_name[item.impact_node_name].id
        if item.impact_company_code:
            company = company_by_key.get(item.impact_company_code.strip().upper())
            if company:
                row.impact_company_id = company.id

    for item in result.new_sources:
        source_row = None
        if item.source_url:
            source_row = db.scalar(select(IndustryChainEvidence).where(IndustryChainEvidence.chain_id == chain.id, IndustryChainEvidence.source_url == item.source_url))
        if source_row is None:
            source_row = db.scalar(select(IndustryChainEvidence).where(IndustryChainEvidence.chain_id == chain.id, IndustryChainEvidence.title == item.title, IndustryChainEvidence.evidence_date == item.evidence_date))
        if source_row is None:
            source_row = IndustryChainEvidence(chain_id=chain.id, title=item.title, evidence_date=item.evidence_date)
            db.add(source_row)
            db.flush()
        source_row.title = item.title
        source_row.content = item.content
        source_row.source_name = item.source_name
        source_row.source_url = item.source_url
        source_row.impact_level = item.impact_level if item.impact_level in LEVELS else "中"
        source_row.source_tier = item.source_tier if item.source_tier in SOURCE_TIERS else "Codex判断"
        source_row.verification_status = item.verification_status if item.verification_status in SOURCE_VERIFICATION_STATUSES else "待验证"
        source_row.evidence_date = item.evidence_date
        for name in item.node_names:
            node = node_by_name.get(name)
            if not node:
                continue
            existing_link = db.scalar(select(IndustryTrendNodeSource).where(IndustryTrendNodeSource.node_id == node.id, IndustryTrendNodeSource.evidence_id == source_row.id))
            if existing_link is None:
                db.add(IndustryTrendNodeSource(chain_id=chain.id, node_id=node.id, evidence_id=source_row.id))
        changed = True

    for item in result.updates:
        existing_update = db.scalar(select(IndustryTrendUpdate).where(IndustryTrendUpdate.chain_id == chain.id, IndustryTrendUpdate.update_date == item.update_date, IndustryTrendUpdate.content == item.content))
        if existing_update:
            continue
        db.add(IndustryTrendUpdate(
            chain_id=chain.id,
            update_date=item.update_date,
            content=item.content,
            source_name=item.source_name,
            source_url=item.source_url,
            impact=item.impact,
            next_verification=item.next_verification,
            causal_stage=item.causal_stage,
            evidence_type=item.evidence_type,
            signal_status=item.signal_status,
            buyer_group=item.buyer_group,
            market_response=item.market_response,
            sell_pressure=item.sell_pressure,
            counter_evidence=item.counter_evidence,
            affects_phase=chain.phase != old_phase,
            affects_decision=bool(core_changes),
        ))
        changed = True

    summary_parts = [result.update_summary]
    if result.invalidated_information:
        summary_parts.append("失效：" + "；".join(result.invalidated_information))
    if result.contradictions:
        summary_parts.append("冲突：" + "；".join(result.contradictions))
    if not db.scalar(select(IndustryTrendUpdate).where(IndustryTrendUpdate.chain_id == chain.id, IndustryTrendUpdate.update_date == date.today(), IndustryTrendUpdate.content == result.update_summary)):
        db.add(IndustryTrendUpdate(
            chain_id=chain.id,
            update_date=date.today(),
            content=result.update_summary,
            impact="\n".join(summary_parts[1:]) or ("全面检查完成，无实质变化。" if result.no_material_change else "全面更新草稿已由用户确认。"),
            next_verification="；".join(item.gap for item in result.coverage if item.gap) or None,
            affects_phase=chain.phase != old_phase,
            affects_decision=bool(core_changes),
        ))
        changed = True
    if changed and not result.no_material_change:
        chain.last_change_at = now_utc()


@router.put("/{chain_id}/draft")
def save_draft(chain_id: int, payload: DraftPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    _chain_or_404(db, chain_id)
    row = _upsert_draft(db, chain_id, payload.draft_type, payload.source, payload.payload)
    db.commit()
    return {"id": row.id, "draft_type": row.draft_type, "source": row.source, "payload": payload.payload, "updated_at": row.updated_at}


@router.delete("/{chain_id}/draft")
def delete_draft(chain_id: int, db: Session = Depends(get_db)) -> dict[str, str]:
    _chain_or_404(db, chain_id)
    db.execute(delete(IndustryTrendDraft).where(IndustryTrendDraft.chain_id == chain_id))
    db.commit()
    return {"status": "ok", "message": "产业研究草稿已丢弃"}


def _apply_result(db: Session, chain: IndustryChain, result: CodexResult) -> None:
    chain.summary = result.summary
    chain.investment_logic = result.investment_logic
    chain.change_summary = result.change_summary
    chain.why_now = result.why_now
    chain.drivers_json = _json_dump(result.drivers)
    chain.expected_duration = result.expected_duration
    chain.risk = result.risk
    chain.phase = _choice(result.phase, PHASES, "产业阶段")
    chain.strength = result.strength
    chain.attention_level = _choice(result.attention_level, ATTENTION_LEVELS, "关注等级")
    chain.direction_verdict = _choice(result.direction_verdict, VERDICTS, "方向判断")
    chain.stock_verdict = _choice(result.stock_verdict, VERDICTS, "股票判断")
    chain.timing_verdict = _choice(result.timing_verdict, VERDICTS, "时点判断")
    chain.pricing_status = _choice(result.pricing_status, PRICING_STATUSES, "定价状态")
    chain.priced_in = result.priced_in
    chain.not_priced_in = result.not_priced_in
    chain.next_signal = result.next_signal
    chain.invalidation = result.invalidation
    chain.phase_entered_at = date.today()
    chain.status = "paused" if chain.attention_level == "暂停" else "active"

    db.execute(delete(IndustryTrendEdge).where(IndustryTrendEdge.chain_id == chain.id))
    db.execute(delete(IndustryChainTask).where(IndustryChainTask.chain_id == chain.id))
    db.execute(delete(IndustryTrendCatalyst).where(IndustryTrendCatalyst.chain_id == chain.id))
    db.execute(delete(IndustryTrendUpdate).where(IndustryTrendUpdate.chain_id == chain.id))
    db.execute(delete(IndustryChainEvidence).where(IndustryChainEvidence.chain_id == chain.id))
    db.execute(delete(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain.id))
    db.execute(delete(IndustryTrendNode).where(IndustryTrendNode.chain_id == chain.id))
    db.flush()

    nodes: dict[str, IndustryTrendNode] = {}
    for index, item in enumerate(result.nodes):
        node_type = NODE_TYPE_ALIASES.get(item.node_type, item.node_type)
        node_type = node_type if node_type in NODE_TYPES else "光互联产品"
        row = IndustryTrendNode(
            chain_id=chain.id,
            name=item.name,
            node_type=node_type,
            plain_explanation=item.plain_explanation,
            value_flow=item.value_flow,
            watch_signal=item.watch_signal,
            maturity_status=item.maturity_status if item.maturity_status in MATURITY_STATUSES else None,
            market_space=item.market_space,
            tech_barrier=item.tech_barrier,
            competition=item.competition,
            profit_elasticity=item.profit_elasticity if item.profit_elasticity in LEVELS else None,
            localization=item.localization if item.localization in LEVELS else None,
            investment_importance=item.investment_importance if item.investment_importance in LEVELS else None,
            sort_order=(index + 1) * 10,
        )
        db.add(row)
        db.flush()
        nodes[item.name] = row
    for edge in result.edges:
        source = nodes.get(edge.from_name)
        target = nodes.get(edge.to_name)
        if source and target and source.id != target.id:
            db.add(IndustryTrendEdge(chain_id=chain.id, from_node_id=source.id, to_node_id=target.id))

    companies: dict[str, IndustryChainCompany] = {}
    primary: IndustryChainCompany | None = None
    for index, item in enumerate(result.companies):
        try:
            identity = _company_identity(db, item.code, item.name, item.market, item.exchange)
        except HTTPException:
            continue
        tracking = item.tracking_status if item.tracking_status in TRACKING_STATUSES else "观察"
        verification = item.verification_status if item.verification_status in VERIFICATION_STATUSES else "未验证"
        pricing = item.pricing_status if item.pricing_status in PRICING_STATUSES else "部分定价"
        row = IndustryChainCompany(
            chain_id=chain.id,
            code=identity["code"],
            name=identity["name"],
            exchange=identity["exchange"],
            full_code=identity["full_code"],
            market=identity["market"],
            external_url=item.external_url,
            position=item.position,
            company_standing=item.company_standing,
            core_logic=item.core_advantage,
            benefit_directness=item.benefit_directness,
            profit_path=item.profit_path,
            verification_status=verification,
            tracking_status=tracking,
            pricing_status=pricing,
            is_global_leader=item.is_global_leader,
            is_domestic_alternative=item.is_domestic_alternative and identity["market"] == "A股",
            is_primary=item.is_primary and identity["market"] == "A股" and primary is None,
            primary_reason=item.primary_reason,
            node_ids_json=_json_dump([nodes[name].id for name in item.node_names if name in nodes]),
            main_risk=item.main_risk,
            market_implied_expectation=item.market_implied_expectation,
            evidence_based_expectation=item.evidence_based_expectation,
            expectation_gap_status=item.expectation_gap_status,
            expectation_gap_reason=item.expectation_gap_reason,
            expectation_trigger=item.expectation_trigger,
            expectation_invalidation=item.expectation_invalidation,
            expectation_evidence_growth_pct=item.expectation_evidence_growth_pct,
            expectation_evidence_acceleration_pct=item.expectation_evidence_acceleration_pct,
            expectation_as_of=date.today() if item.expectation_gap_status != "无法判断" else None,
            sort_order=(index + 1) * 10,
        )
        db.add(row)
        db.flush()
        for key in _company_lookup_keys(row.code, row.full_code):
            companies[key] = row
        if row.is_primary:
            primary = row
    if primary is None:
        primary = next((row for row in companies.values() if (row.market or "A股") == "A股"), None)
    if primary is not None:
        primary.is_primary = True
    chain.primary_company_id = primary.id if primary else None

    for index, item in enumerate(result.validations):
        company = None
        if item.company_code:
            company = companies.get(item.company_code.strip().upper())
        node = nodes.get(item.node_name or "")
        db.add(IndustryChainTask(
            chain_id=chain.id,
            company_id=company.id if company else None,
            node_id=node.id if node else None,
            title=item.name,
            description=item.criteria,
            criteria=item.criteria,
            current_result=item.current_result,
            status=item.status if item.status in VERIFICATION_STATUSES else "未验证",
            due_date=item.target_date,
            source_name=item.source_name,
            source_url=item.source_url,
            priority="中",
            sort_order=item.sort_order if item.sort_order != 100 else (index + 1) * 10,
        ))
    for index, item in enumerate(result.catalysts):
        node = nodes.get(item.impact_node_name or "")
        company = None
        if item.impact_company_code:
            company = companies.get(item.impact_company_code.strip().upper())
        db.add(IndustryTrendCatalyst(
            chain_id=chain.id,
            event_name=item.event_name,
            expected_time=item.expected_time,
            event_type=item.event_type,
            impact_node_id=node.id if node else None,
            impact_company_id=company.id if company else None,
            importance=item.importance if item.importance in CATALYST_IMPORTANCE_LEVELS else "中",
            status=item.status if item.status in CATALYST_STATUSES else "预期",
            impact=item.impact,
            source_name=item.source_name,
            source_url=item.source_url,
            sort_order=item.sort_order if item.sort_order != 100 else (index + 1) * 10,
        ))
    for item in result.updates:
        db.add(IndustryTrendUpdate(
            chain_id=chain.id,
            update_date=item.update_date,
            content=item.content,
            source_name=item.source_name,
            source_url=item.source_url,
            impact=item.impact,
            next_verification=item.next_verification,
            causal_stage=item.causal_stage,
            evidence_type=item.evidence_type,
            signal_status=item.signal_status,
            buyer_group=item.buyer_group,
            market_response=item.market_response,
            sell_pressure=item.sell_pressure,
            counter_evidence=item.counter_evidence,
        ))
    for item in result.sources:
        source_row = IndustryChainEvidence(
            chain_id=chain.id,
            title=item.title,
            content=item.content,
            source_name=item.source_name,
            source_url=item.source_url,
            impact_level=item.impact_level if item.impact_level in LEVELS else "中",
            source_tier=item.source_tier if item.source_tier in SOURCE_TIERS else "官方硬证据",
            verification_status=item.verification_status if item.verification_status in SOURCE_VERIFICATION_STATUSES else "单一来源",
            evidence_date=item.evidence_date,
        )
        db.add(source_row)
        db.flush()
        for node_name in item.node_names:
            node = nodes.get(node_name)
            if node:
                db.add(IndustryTrendNodeSource(
                    chain_id=chain.id,
                    node_id=node.id,
                    evidence_id=source_row.id,
                ))
    chain.last_change_at = now_utc()


@router.post("/{chain_id}/draft/apply")
def apply_draft(chain_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    draft = db.scalar(select(IndustryTrendDraft).where(IndustryTrendDraft.chain_id == chain_id))
    if not draft:
        raise HTTPException(status_code=404, detail="没有待应用草稿")
    payload = _json_load(draft.payload_json, {})
    material_ids = [item for item in payload.get("_material_ids", []) if isinstance(item, int)]
    try:
        if draft.draft_type == "update" and payload.get("delta_version") == "1":
            delta = CodexUpdateDelta.model_validate(
                {key: value for key, value in payload.items() if not key.startswith("_")}
            )
            _apply_update_delta(db, chain, delta, _research_setting(db, chain_id))
        else:
            result = CodexResult.model_validate(payload)
            _apply_result(db, chain, result)
    except (ValueError, HTTPException) as exc:
        raise HTTPException(status_code=400, detail=f"草稿结构不完整：{exc}") from exc
    if draft.source.startswith("codex"):
        latest_job = db.scalar(
            select(IndustryTrendGenerationJob)
            .where(
                IndustryTrendGenerationJob.chain_id == chain_id,
                IndustryTrendGenerationJob.job_type == draft.draft_type,
                IndustryTrendGenerationJob.status == "succeeded",
            )
            .order_by(desc(IndustryTrendGenerationJob.id))
        )
        if latest_job and latest_job.phase == "草稿待确认":
            latest_job.phase = "已应用"
    db.delete(draft)
    if material_ids:
        db.execute(
            IndustryTrendMaterial.__table__.update()
            .where(
                IndustryTrendMaterial.chain_id == chain_id,
                IndustryTrendMaterial.id.in_(material_ids),
                IndustryTrendMaterial.status == "待处理",
            )
            .values(status="已纳入", processed_at=now_utc(), updated_at=now_utc())
        )
    db.flush()
    _save_version(db, chain)
    db.commit()
    return _detail_out(db, chain)


def _job_out(row: IndustryTrendGenerationJob) -> dict[str, Any]:
    return {
        "id": row.id,
        "chain_id": row.chain_id,
        "job_type": row.job_type,
        "status": row.status,
        "phase": row.phase,
        "error_message": _generation_error_summary(row.error_message) if row.error_message else None,
        "update_mode": "全面更新" if row.job_type == "update" else "首次研究",
        "input_token_estimate": row.input_token_estimate,
        "output_token_estimate": row.output_token_estimate,
        "total_token_estimate": row.input_token_estimate + row.output_token_estimate,
        "change_count": row.change_count,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "result": _json_load(row.result_json, None),
    }


def _compact_update_context(current: dict[str, Any]) -> dict[str, Any]:
    core_keys = (
        "name", "summary", "phase", "strength", "attention_level", "direction_verdict", "stock_verdict",
        "timing_verdict", "pricing_status", "investment_logic", "change_summary", "why_now", "drivers",
        "expected_duration", "risk", "priced_in", "not_priced_in", "next_signal", "invalidation", "last_change_at",
    )
    return {
        "core": {key: current.get(key) for key in core_keys},
        "nodes": [{key: item.get(key) for key in ("name", "node_type", "maturity_status", "watch_signal", "investment_importance", "updated_at")} for item in current.get("nodes", [])],
        "companies": [{key: item.get(key) for key in ("name", "code", "full_code", "market", "position", "verification_status", "tracking_status", "pricing_status", "is_global_leader", "is_domestic_alternative", "is_primary", "node_ids", "updated_at")} for item in current.get("companies", [])],
        "validations": [{key: item.get(key) for key in ("name", "criteria", "current_result", "status", "target_date", "source_url", "updated_at")} for item in current.get("validations", [])],
        "catalysts": [{key: item.get(key) for key in ("event_name", "expected_time", "event_type", "importance", "status", "impact", "source_url", "updated_at")} for item in current.get("catalysts", [])],
        "known_sources": [{key: item.get(key) for key in ("title", "source_name", "source_url", "source_tier", "verification_status", "evidence_state", "valid_until", "conflict_note", "evidence_date", "node_ids")} for item in current.get("sources", [])],
        "recent_updates": [{key: item.get(key) for key in (
            "update_date", "content", "impact", "next_verification", "source_url",
            "causal_stage", "evidence_type", "signal_status", "buyer_group",
            "market_response", "sell_pressure", "counter_evidence",
        )} for item in current.get("updates", [])[:20]],
    }


def _codex_prompt(
    chain: IndustryChain,
    input_data: dict[str, Any],
    current: dict[str, Any] | None,
    setting_data: dict[str, Any] | None = None,
) -> str:
    if input_data.get("job_type") == "update" and current is not None:
        compact = _compact_update_context(current)
        settings = setting_data or _research_setting_out(None, chain.id)
        return f"""
你正在执行产业趋势工作台唯一的“全面更新”任务。全面指覆盖全部产业节点和验证维度；输出必须是增量差异，禁止重写未变化的旧资料。
当前日期：{date.today().isoformat()}
产业：{chain.name}
上次完成研究：{settings.get('last_researched_at') or '无记录'}
用户本次线索：{input_data.get('clue') or '无'}
用户本次临时要求：{input_data.get('requirements') or '无'}
更新中心待处理材料：{_json_dump(input_data.get('pending_materials') or [])}
长期研究设定：{_json_dump(settings)}
现有研究压缩索引：{_json_dump(compact)}

必须逐项检查：需求与资本开支、价格与订单、技术路线、产能与供给、竞争格局、公司收入利润和毛利率、关键事件、市场定价、风险与证伪；coverage每个维度必须有一项。公司推荐必须同时检查两条线：全球绝对优势公司、国内可替代公司。
优先搜索上次研究之后出现的公告、财报、公司官网、政府监管和行业组织资料；priority_nodes和priority_companies只是重点，不得因此跳过其他节点。excluded_keywords中的内容不纳入结果。
正式结论优先使用一手来源。只有至少两个独立来源支持同一结论才标记已互证；雪球、同花顺、媒体和行情只作为市场线索。发现冲突必须写入contradictions，证据不足必须写入coverage.gap，不得猜测补齐。
known_sources已经保存：URL或事实没有变化时不要重复输出。new_sources只输出真正新增或内容发生变化的来源，并关联现有或新增节点。
nodes、companies、validations、catalysts只输出新增或变化项；action明确写新增或更新。companies中is_global_leader只用于具有全球份额、技术、客户或交付绝对优势且有硬依据的公司；is_domestic_alternative只用于能够替代海外供应、替代路径和验证节点明确的A股公司。两项都不能仅凭“龙头”称号填写。edges_add只输出新增连线。core_patch只填写确实需要调整的正式字段，没有变化则保持为空。
如果全面检查后没有实质变化，no_material_change=true，仍须填写coverage和简短update_summary，其余变化数组保持为空。
不得删除现有节点、公司或证据。不得自动覆盖正式研究；结果只进入草稿，用户确认后按差异合并。
预计总Token目标不超过{settings.get('token_budget', 24000)}，优先保留硬事实、变化、冲突和验证缺口，省略行业常识复述。
不要加载旧Skill、旧评分体系或旧模板。输出必须严格符合JSON Schema，不要输出Markdown或额外说明。
""".strip()

    context = _json_dump(current) if current else "无现有研究"
    return f"""
你正在为个人机构级产业趋势投资研究工作台生成结构化研究草稿。投资决策仍以A股表达为主，但要用全球公司校验产业格局、技术路线和供需变化。
当前日期：{date.today().isoformat()}。
产业名称：{chain.name}
任务类型：{input_data.get('job_type')}
用户线索：{input_data.get('clue') or '无'}
关注股票：{input_data.get('focus_stocks') or []}
补充要求：{input_data.get('requirements') or '无'}
现有研究：{context}

必须自上而下完成：发现产业变化、验证趋势、拆解价值链、分别标注“全球绝对优势公司”和“国内可替代公司”、比较A股候选、选出唯一A股交易首选或明确无合格表达、判断市场定价与当前时点、给出关键催化事件、按先后顺序组织验证路线图并给出证伪条件。
优先使用公告、财报、公司官网、政府、监管机构和行业组织等一手来源；每个核心事实必须给出可打开链接和日期。二手来源必须谨慎标注，不能把推测写成事实。
sources必须填写source_tier、verification_status和node_names：source_tier只允许官方硬证据/公司披露/行业标准/市场数据/雪球线索/Codex判断；verification_status只允许已互证/单一来源/市场线索/待验证；node_names必须关联到本次nodes中的具体节点。只有至少两个相互独立的一手来源支持同一结论时才能标记已互证，雪球或行情只能作为市场线索，不能把多个转载当作多个独立证据。
不要加载或沿用任何旧Skill、旧评分模型或旧页面模板。不要使用百分制股票评分。
direction_verdict、stock_verdict、timing_verdict只允许通过/观察/否决；phase只允许观察期/萌芽期/验证期/增长期/爆发期/成熟期/退潮期；pricing_status只允许未定价/部分定价/充分定价。
nodes的node_type只允许需求驱动/网络与系统/光互联产品/核心器件/制造与配套。每个节点必须用plain_explanation说明“这是什么”，用value_flow说明“钱为什么流到这里”，用watch_signal说明“后面看什么验证”，maturity_status只允许前沿储备/验证中/小批量/放量中/成熟应用。edges使用from_name和to_name连接节点。companies的market只允许A股/美股/台湾/韩国/日本/其他；海外公司必须填写exchange和官方external_url。is_global_leader表示全球份额、技术壁垒、客户或规模交付具有绝对优势，必须在company_standing和core_advantage中写清依据；is_domestic_alternative只允许A股公司，必须说明替代海外哪一环节、当前认证/订单/量产进度。is_primary仍只是唯一A股交易表达，与前两类推荐逻辑分开。个股预期差必须分开填写market_implied_expectation（当前市值需要相信什么）、evidence_based_expectation（硬证据实际支持什么）、expectation_gap_status（只允许正向预期差/基本匹配/负向预期差/无法判断）、expectation_gap_reason、expectation_trigger和expectation_invalidation；缺少当前市值、盈利锚或验证指标时必须写无法判断，不能仅凭涨跌给正向预期差。
事件不是独立投资方法。catalysts只记录推动产业逻辑从可能走向确定、或推动产业进入下一阶段的关键事件；status只允许预期/确认/兑现，importance只允许高/中/低，并关联影响节点或股票。validations必须按照产业逻辑→需求/资本开支→订单/价格→公司收入利润的验证顺序填写sort_order。
updates如果能够解释上涨或退潮，必须按真实变化/认知扩散/资金进入/筹码交换/拥挤退潮标注causal_stage；硬事实、市场线索、盘面确认、市场推断必须分开，无法确认的买方或卖压保持为空，不得把行情结果倒推成事实。
如果任务类型是update：update_summary、invalidated_information、phase_change_reason、stock_ranking_change、primary_company_assessment、pricing_change、validation_changes、decision_change_reason只写本次新增或变化；其余公共字段必须输出合并现有研究后的完整新版本，避免用户确认草稿时丢失旧内容。
输出必须严格符合JSON Schema，不要输出Markdown或额外说明。
""".strip()


_job_processes: dict[int, subprocess.Popen[str]] = {}
_job_lock = threading.Lock()


def _codex_executable() -> str | None:
    configured = os.environ.get("CODEX_CLI_PATH")
    if configured and Path(configured).is_file():
        return configured
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if bundled.is_file():
        return str(bundled)
    return shutil.which("codex")


def _codex_disabled_personal_skills_config() -> str:
    skills_root = Path.home() / ".codex" / "skills"
    paths = sorted(
        path
        for path in skills_root.glob("**/SKILL.md")
        if ".system" not in path.parts
    )
    entries = ",".join(
        f'{{path={json.dumps(str(path), ensure_ascii=False)},enabled=false}}'
        for path in paths
    )
    return f"skills.config=[{entries}]"


def _codex_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Convert Pydantic JSON Schema into the strict object form required by Codex."""
    schema = model.model_json_schema()

    def normalize(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                normalize(item)
            return
        if not isinstance(value, dict):
            return
        value.pop("default", None)
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["additionalProperties"] = False
            value["required"] = list(properties)
        elif value.get("type") == "object":
            value["additionalProperties"] = False
        for item in value.values():
            normalize(item)

    normalize(schema)
    return schema


def _generation_error_summary(message: str) -> str:
    normalized = re.sub(r"\s+", " ", str(message or "")).strip()
    lower = normalized.lower()
    if "invalid_json_schema" in lower or "invalid schema for response_format" in lower or "additionalproperties" in lower:
        return "生成规则校验失败，请重新发起全面更新。"
    if "invalid_request_error" in lower:
        return "Codex请求格式校验失败，请重新发起全面更新。"
    if "未找到本地codex命令" in lower:
        return "本地Codex服务不可用，请检查程序后重试。"
    if not normalized:
        return "Codex生成失败，请稍后重试。"
    return normalized[:240] + ("…" if len(normalized) > 240 else "")


def _run_generation_job(job_id: int) -> None:
    with SessionLocal() as db:
        job = db.get(IndustryTrendGenerationJob, job_id)
        if not job or job.status == "cancelled":
            return
        chain = db.get(IndustryChain, job.chain_id)
        if not chain:
            job.status = "failed"
            job.error_message = "产业不存在"
            job.finished_at = now_utc()
            db.commit()
            return
        job.status = "running"
        job.phase = "联网检索与形成判断"
        job.started_at = now_utc()
        db.commit()
        input_data = _json_load(job.input_json, {})
        current = _detail_out(db, chain) if job.job_type == "update" else None
        if current:
            current.pop("draft", None)
        setting = _research_setting(db, chain.id, create=True) if job.job_type == "update" else None
        setting_data = _research_setting_out(setting, chain.id) if setting else None
        prompt = _codex_prompt(chain, input_data, current, setting_data)
        job_type = job.job_type
        job.input_token_estimate = max(1, len(prompt) // 4)
        db.commit()

    try:
        test_result = os.environ.get("INDUSTRY_TREND_CODEX_TEST_RESULT")
        if test_result:
            raw_result = json.loads(test_result)
        else:
            executable = _codex_executable()
            if not executable:
                raise RuntimeError("未找到本地Codex命令")
            runtime_dir = get_data_dir() / "industry-trend-codex"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            result_model = CodexUpdateDelta if job_type == "update" else CodexResult
            schema_path = runtime_dir / ("industry-trend-update-schema.json" if job_type == "update" else "industry-trend-schema.json")
            schema_path.write_text(_json_dump(_codex_output_schema(result_model)), encoding="utf-8")
            output_path = runtime_dir / f"job-{job_id}-result.json"
            command = [
                executable,
                "--search",
                "-a", "never",
                "-s", "read-only",
                "-C", str(runtime_dir),
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "-c", _codex_disabled_personal_skills_config(),
                "--disable", "plugins",
                "--disable", "apps",
                "--disable", "memories",
                "--disable", "chronicle",
                "--disable", "multi_agent",
                "--disable", "tool_suggest",
                "--output-schema", str(schema_path),
                "-o", str(output_path),
                prompt,
            ]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            with _job_lock:
                _job_processes[job_id] = process
            with SessionLocal() as db:
                job = db.get(IndustryTrendGenerationJob, job_id)
                if job:
                    job.process_id = process.pid
                    db.commit()
            _, stderr = process.communicate(timeout=600)
            with _job_lock:
                _job_processes.pop(job_id, None)
            if process.returncode != 0:
                raise RuntimeError((stderr or "Codex生成失败")[-1200:])
            raw_text = output_path.read_text(encoding="utf-8").strip()
            raw_result = json.loads(raw_text)
        result_model = CodexUpdateDelta if job_type == "update" else CodexResult
        validated = result_model.model_validate(raw_result)
        payload = validated.model_dump(mode="json")
        if job_type == "update":
            if not isinstance(validated, CodexUpdateDelta):
                raise TypeError("全面更新结果类型错误")
            delta = validated
            change_count = (
                len(delta.nodes) + len(delta.edges_add) + len(delta.companies) + len(delta.validations)
                + len(delta.catalysts) + len(delta.updates) + len(delta.new_sources)
                + len(delta.invalidated_information) + len(delta.contradictions)
                + (1 if delta.core_patch.model_dump(exclude_none=True) else 0)
            )
            payload["_material_ids"] = [
                item for item in input_data.get("pending_material_ids", []) if isinstance(item, int)
            ]
        else:
            change_count = 0
        with SessionLocal() as db:
            job = db.get(IndustryTrendGenerationJob, job_id)
            if not job or job.status == "cancelled":
                return
            _upsert_draft(db, job.chain_id, job.job_type, "codex_web", payload)
            job.status = "succeeded"
            job.phase = "草稿待确认"
            job.result_json = _json_dump(payload)
            job.output_token_estimate = max(1, len(job.result_json) // 4)
            job.change_count = change_count
            job.finished_at = now_utc()
            setting = _research_setting(db, job.chain_id, create=True) if job.job_type == "update" else None
            if setting:
                setting.last_researched_at = job.finished_at
            db.commit()
    except subprocess.TimeoutExpired:
        with _job_lock:
            process = _job_processes.pop(job_id, None)
        if process:
            process.kill()
        _fail_job(job_id, "Codex生成超过10分钟，任务已终止")
    except Exception as exc:  # noqa: BLE001
        with _job_lock:
            _job_processes.pop(job_id, None)
        _fail_job(job_id, str(exc))


def _fail_job(job_id: int, message: str) -> None:
    with SessionLocal() as db:
        job = db.get(IndustryTrendGenerationJob, job_id)
        if not job or job.status == "cancelled":
            return
        job.status = "failed"
        job.phase = "生成失败"
        job.error_message = _generation_error_summary(message)
        job.result_json = _json_dump({"technical_error": str(message)[:3000]})
        job.finished_at = now_utc()
        db.commit()


@router.post("/generate")
def generate_trend(payload: GeneratePayload, db: Session = Depends(get_db)) -> dict[str, Any]:
    if payload.chain_id:
        chain = _chain_or_404(db, payload.chain_id)
    else:
        if not payload.name or not payload.name.strip():
            raise HTTPException(status_code=400, detail="首次研究必须填写产业名称")
        existing = db.scalar(select(IndustryChain).where(IndustryChain.name == payload.name.strip()))
        if existing:
            chain = existing
        else:
            chain = IndustryChain(
                name=payload.name.strip(),
                phase="观察期",
                strength=20,
                attention_level="观察",
                direction_verdict="观察",
                stock_verdict="观察",
                timing_verdict="观察",
                overall_verdict="观察",
                pricing_status="部分定价",
                phase_entered_at=date.today(),
                status="active",
            )
            db.add(chain)
            db.flush()
    active = db.scalar(select(IndustryTrendGenerationJob).where(IndustryTrendGenerationJob.chain_id == chain.id, IndustryTrendGenerationJob.status.in_(JOB_STATUSES_ACTIVE)).order_by(desc(IndustryTrendGenerationJob.id)))
    if active:
        raise HTTPException(status_code=409, detail="该产业已有生成任务正在运行")
    input_data = payload.model_dump(mode="json")
    if payload.job_type == "update":
        pending_materials = list(
            db.scalars(
                select(IndustryTrendMaterial)
                .where(
                    IndustryTrendMaterial.chain_id == chain.id,
                    IndustryTrendMaterial.status == "待处理",
                )
                .order_by(IndustryTrendMaterial.material_date, IndustryTrendMaterial.id)
            )
        )
        input_data["pending_materials"] = [_material_out(row) for row in pending_materials]
        input_data["pending_material_ids"] = [row.id for row in pending_materials]
    job = IndustryTrendGenerationJob(
        chain_id=chain.id,
        job_type=payload.job_type,
        status="queued",
        phase="等待开始",
        input_json=_json_dump(input_data),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    threading.Thread(target=_run_generation_job, args=(job.id,), name=f"industry-trend-codex-{job.id}", daemon=True).start()
    return _job_out(job)


@router.get("/{chain_id}/generation-jobs")
def list_generation_jobs(chain_id: int, limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)) -> dict[str, Any]:
    _chain_or_404(db, chain_id)
    rows = db.scalars(
        select(IndustryTrendGenerationJob)
        .where(IndustryTrendGenerationJob.chain_id == chain_id)
        .order_by(desc(IndustryTrendGenerationJob.id))
        .limit(limit)
    ).all()
    return {"items": [_job_out(row) for row in rows]}


@router.get("/generation-jobs/{job_id}")
def get_generation_job(job_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(IndustryTrendGenerationJob, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return _job_out(row)


@router.post("/generation-jobs/{job_id}/cancel")
def cancel_generation_job(job_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(IndustryTrendGenerationJob, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    if row.status not in JOB_STATUSES_ACTIVE:
        return _job_out(row)
    with _job_lock:
        process = _job_processes.pop(job_id, None)
    if process:
        process.terminate()
    row.status = "cancelled"
    row.phase = "已取消"
    row.finished_at = now_utc()
    db.commit()
    return _job_out(row)


@router.get("/{chain_id}/versions")
def list_versions(chain_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    _chain_or_404(db, chain_id)
    rows = db.scalars(select(IndustryTrendVersion).where(IndustryTrendVersion.chain_id == chain_id).order_by(desc(IndustryTrendVersion.revision))).all()
    return {"items": [{"id": row.id, "revision": row.revision, "created_at": row.created_at, "snapshot": _json_load(row.snapshot_json, {})} for row in rows]}


@router.post("/{chain_id}/versions/{version_id}/restore")
def restore_version(chain_id: int, version_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    chain = _chain_or_404(db, chain_id)
    version = db.scalar(select(IndustryTrendVersion).where(IndustryTrendVersion.id == version_id, IndustryTrendVersion.chain_id == chain_id))
    if not version:
        raise HTTPException(status_code=404, detail="历史版本不存在")
    snapshot = _json_load(version.snapshot_json, {})
    result = CodexResult(
        summary=snapshot.get("summary") or "",
        investment_logic=snapshot.get("investment_logic") or "",
        change_summary=snapshot.get("change_summary") or "",
        why_now=snapshot.get("why_now") or "",
        drivers=snapshot.get("drivers") or [],
        expected_duration=snapshot.get("expected_duration"),
        risk=snapshot.get("risk"),
        phase=snapshot.get("phase") or "观察期",
        strength=snapshot.get("strength") or 0,
        attention_level=snapshot.get("attention_level") or "观察",
        direction_verdict=snapshot.get("direction_verdict") or "观察",
        stock_verdict=snapshot.get("stock_verdict") or "观察",
        timing_verdict=snapshot.get("timing_verdict") or "观察",
        pricing_status=snapshot.get("pricing_status") or "部分定价",
        priced_in=snapshot.get("priced_in"),
        not_priced_in=snapshot.get("not_priced_in"),
        next_signal=snapshot.get("next_signal"),
        invalidation=snapshot.get("invalidation"),
        nodes=[CodexNode(**{key: value for key, value in item.items() if key in CodexNode.model_fields}) for item in snapshot.get("nodes", [])],
        edges=[{
            "from_name": next((node["name"] for node in snapshot.get("nodes", []) if node.get("id") == item.get("from_node_id")), ""),
            "to_name": next((node["name"] for node in snapshot.get("nodes", []) if node.get("id") == item.get("to_node_id")), ""),
        } for item in snapshot.get("edges", [])],
        companies=[CodexCompany(
            code=item.get("code") or item.get("full_code") or "",
            name=item.get("name") or "",
            market=item.get("market") or "A股",
            exchange=item.get("exchange"),
            external_url=item.get("external_url"),
            node_names=[node["name"] for node in snapshot.get("nodes", []) if node.get("id") in item.get("node_ids", [])],
            position=item.get("position"),
            company_standing=item.get("company_standing"),
            core_advantage=item.get("core_advantage"),
            benefit_directness=item.get("benefit_directness"),
            profit_path=item.get("profit_path"),
            verification_status=item.get("verification_status") or "未验证",
            tracking_status=item.get("tracking_status") or "观察",
            pricing_status=item.get("pricing_status") or "部分定价",
            is_global_leader=bool(item.get("is_global_leader")),
            is_domestic_alternative=bool(item.get("is_domestic_alternative")),
            is_primary=item.get("is_primary") or False,
            primary_reason=item.get("primary_reason"),
            main_risk=item.get("main_risk"),
            market_implied_expectation=item.get("market_implied_expectation"),
            evidence_based_expectation=item.get("evidence_based_expectation"),
            expectation_gap_status=item.get("expectation_gap_status") or "无法判断",
            expectation_gap_reason=item.get("expectation_gap_reason"),
            expectation_trigger=item.get("expectation_trigger"),
            expectation_invalidation=item.get("expectation_invalidation"),
            expectation_evidence_growth_pct=item.get("expectation_evidence_growth_pct"),
            expectation_evidence_acceleration_pct=item.get("expectation_evidence_acceleration_pct"),
        ) for item in snapshot.get("companies", []) if item.get("code") or item.get("full_code")],
        validations=[CodexValidation(
            name=item.get("name") or "未命名指标",
            criteria=item.get("criteria"),
            current_result=item.get("current_result"),
            status=item.get("status") or "未验证",
            company_code=next(
                (
                    company.get("full_code") or company.get("code")
                    for company in snapshot.get("companies", [])
                    if company.get("id") == item.get("company_id")
                ),
                None,
            ),
            node_name=next(
                (node.get("name") for node in snapshot.get("nodes", []) if node.get("id") == item.get("node_id")),
                None,
            ),
            target_date=item.get("target_date"),
            source_name=item.get("source_name"),
            source_url=item.get("source_url"),
            sort_order=item.get("sort_order") or 100,
        ) for item in snapshot.get("validations", [])],
        catalysts=[CodexCatalyst(
            event_name=item.get("event_name") or "未命名催化事件",
            expected_time=item.get("expected_time"),
            event_type=item.get("event_type") or "其他",
            impact_node_name=next(
                (node.get("name") for node in snapshot.get("nodes", []) if node.get("id") == item.get("impact_node_id")),
                None,
            ),
            impact_company_code=next(
                (
                    company.get("full_code") or company.get("code")
                    for company in snapshot.get("companies", [])
                    if company.get("id") == item.get("impact_company_id")
                ),
                None,
            ),
            importance=item.get("importance") or "中",
            status=item.get("status") or "预期",
            impact=item.get("impact"),
            source_name=item.get("source_name"),
            source_url=item.get("source_url"),
            sort_order=item.get("sort_order") or 100,
        ) for item in snapshot.get("catalysts", [])],
        updates=[CodexUpdate(
            update_date=item.get("update_date") or date.today(),
            content=item.get("content") or "未命名更新",
            source_name=item.get("source_name"),
            source_url=item.get("source_url"),
            impact=item.get("impact"),
            next_verification=item.get("next_verification"),
            causal_stage=item.get("causal_stage"),
            evidence_type=item.get("evidence_type"),
            signal_status=item.get("signal_status"),
            buyer_group=item.get("buyer_group"),
            market_response=item.get("market_response"),
            sell_pressure=item.get("sell_pressure"),
            counter_evidence=item.get("counter_evidence"),
        ) for item in snapshot.get("updates", [])],
        sources=[CodexSource(
            title=item.get("title") or "未命名来源",
            content=item.get("content"),
            source_name=item.get("source_name"),
            source_url=item.get("source_url"),
            evidence_date=item.get("evidence_date") or date.today(),
            impact_level=item.get("impact_level") or "中",
            source_tier=item.get("source_tier") or "官方硬证据",
            verification_status=item.get("verification_status") or "单一来源",
            node_names=[
                node.get("name")
                for node in snapshot.get("nodes", [])
                if node.get("id") in (item.get("node_ids") or []) and node.get("name")
            ],
        ) for item in snapshot.get("sources", [])],
    )
    _apply_result(db, chain, result)
    chain.catalyst = snapshot.get("catalyst")
    chain.sort_order = snapshot.get("sort_order") or chain.sort_order
    if snapshot.get("phase_entered_at"):
        chain.phase_entered_at = date.fromisoformat(str(snapshot["phase_entered_at"])[:10])
    if snapshot.get("last_change_at"):
        chain.last_change_at = datetime.fromisoformat(str(snapshot["last_change_at"]).replace("Z", "+00:00"))
    _save_version(db, chain)
    db.commit()
    return _detail_out(db, chain)
