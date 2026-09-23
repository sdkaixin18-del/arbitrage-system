from __future__ import annotations

import base64
import json
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from pypinyin import lazy_pinyin
from sqlalchemy import and_, case, delete, desc, func, select, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.factors import (
    BEIJING,
    clean_text,
    fetch_history_frame,
    first_value,
    history_source_order,
    inferred_review_status,
    normalize_code,
    parse_date,
    parse_float,
    review_statuses_for_full_codes,
    tag_to_plain,
    tags_for_full_codes,
)
from app.models import AStock, FactorQuoteSnapshot, SectorIndex, SectorIndexBar, SectorIndexMember, StockDailyBar, now_utc
from app.watchlist_announcements import ensure_stock_universe, exchange_from_code, search_a_stocks

SECTOR_BASE_VALUE = 1000.0
DEFAULT_HISTORY_DAYS = 540
POST_CLOSE_REFRESH_TIME = (15, 10)
STOCK_BAR_BACKGROUND_REFRESH_DELAY_SECONDS = 0.5
STOCK_BAR_POST_CLOSE_RETRY_SECONDS = 5 * 60
BAR_RANGE_DAYS = {
    "1m": 45,
    "3m": 120,
    "6m": 220,
    "1y": 420,
}
_MISSING = object()
_STOCK_BAR_REFRESH_LOCK = threading.Lock()
_STOCK_BAR_REFRESHING: set[str] = set()


def sector_indices_overview(db: Session) -> dict[str, Any]:
    ensure_sector_group_columns(db)
    rows = list(
        db.scalars(
            select(SectorIndex)
            .where(SectorIndex.active.is_(True))
            .order_by(SectorIndex.group_sort_order, SectorIndex.sort_order, SectorIndex.id)
        )
    )
    sector_ids = [row.id for row in rows]
    stats_map, latest_bar_map = sector_summary_maps(db, sector_ids)
    sectors = [
        sector_to_summary(db, row, stats_map.get(row.id, empty_sector_stats()), latest_bar_map.get(row.id))
        for row in rows
    ]
    updated_at = max((sector["updated_at"] for sector in sectors), default=None)
    status = "ok" if sectors else "manual_only"
    return {
        "status": status,
        "source_status": status,
        "updated_at": updated_at,
        "message": f"已维护 {len(sectors)} 个自定义板块指数。" if sectors else "还没有自定义板块指数。",
        "sectors": sectors,
        "groups": grouped_sector_summaries(sectors),
    }


def create_sector_index(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_sector_group_columns(db)
    name = clean_sector_name(payload.get("name"))
    description = clean_optional_text(payload.get("description"))
    parent_name = clean_optional_text(payload.get("parent_name"))
    source_image = clean_optional_text(payload.get("source_image"))
    group_sort_order = int(payload.get("group_sort_order") or 100)
    sort_order = int(payload.get("sort_order") or 100)
    duplicate = db.scalar(select(SectorIndex).where(SectorIndex.name == name))
    if duplicate:
        if duplicate.active:
            raise ValueError("板块名称已存在")
        duplicate.active = True
        duplicate.description = description
        duplicate.parent_name = parent_name
        duplicate.group_sort_order = group_sort_order
        duplicate.source_image = source_image
        duplicate.sort_order = sort_order
        duplicate.updated_at = now_utc()
        db.commit()
        db.refresh(duplicate)
        return sector_to_summary(db, duplicate)
    sector = SectorIndex(
        name=name,
        description=description,
        parent_name=parent_name,
        group_sort_order=group_sort_order,
        source_image=source_image,
        sort_order=sort_order,
        base_value=SECTOR_BASE_VALUE,
    )
    db.add(sector)
    db.commit()
    db.refresh(sector)
    return sector_to_summary(db, sector)


def update_sector_index(db: Session, sector_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_sector_group_columns(db)
    sector = get_sector(db, sector_id)
    if "name" in payload and payload["name"] is not None:
        name = clean_sector_name(payload["name"])
        duplicate = db.scalar(select(SectorIndex).where(SectorIndex.name == name, SectorIndex.id != sector.id))
        if duplicate:
            raise ValueError("板块名称已存在")
        sector.name = name
    if "description" in payload:
        sector.description = clean_optional_text(payload.get("description"))
    if "parent_name" in payload:
        sector.parent_name = clean_optional_text(payload.get("parent_name"))
    if "group_sort_order" in payload and payload["group_sort_order"] is not None:
        sector.group_sort_order = int(payload["group_sort_order"])
    if "source_image" in payload:
        sector.source_image = clean_optional_text(payload.get("source_image"))
    if "sort_order" in payload and payload["sort_order"] is not None:
        sector.sort_order = int(payload["sort_order"])
    if "active" in payload and payload["active"] is not None:
        sector.active = bool(payload["active"])
    sector.updated_at = now_utc()
    db.commit()
    db.refresh(sector)
    return sector_to_summary(db, sector)


def delete_sector_index(db: Session, sector_id: int) -> dict[str, str]:
    sector = get_sector(db, sector_id)
    sector.active = False
    sector.updated_at = now_utc()
    db.commit()
    return {"status": "ok", "message": "板块已隐藏，历史 K 线缓存保留。"}


def sector_index_detail(db: Session, sector_id: int) -> dict[str, Any]:
    sector = get_sector(db, sector_id)
    stats_map, latest_bar_map = sector_summary_maps(db, [sector.id])
    return {
        "sector": sector_to_summary(db, sector, stats_map.get(sector.id, empty_sector_stats()), latest_bar_map.get(sector.id)),
        "members": sector_members_to_out(db, sector.id),
        "recent_returns": sector_bars_to_out(db, sector.id, recent_sector_bars(db, sector.id, limit=60)),
    }


def sector_index_bars(db: Session, sector_id: int, range_name: str = "6m") -> list[dict[str, Any]]:
    get_sector(db, sector_id)
    query = select(SectorIndexBar).where(SectorIndexBar.sector_id == sector_id)
    start = range_start_date(range_name)
    if start:
        query = query.where(SectorIndexBar.trade_date >= start)
    rows = list(db.scalars(query.order_by(SectorIndexBar.trade_date)))
    return sector_bars_to_out(db, sector_id, rows)


def stock_bars(db: Session, full_code: str, range_name: str = "6m", refresh: bool = False) -> list[dict[str, Any]]:
    normalized = normalize_full_code(full_code)
    if not normalized:
        raise ValueError("股票代码无效")
    stock = db.scalar(select(AStock).where(AStock.full_code == normalized))
    if not stock:
        ensure_stock_universe(db)
        stock = db.scalar(select(AStock).where(AStock.full_code == normalized))
    if not stock:
        raise ValueError("股票不存在")
    cached_rows = cached_stock_bar_rows(db, normalized)
    snapshot = db.scalar(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code == normalized))
    if cached_rows and not refresh:
        if should_refresh_stock_bar_cache_after_close(cached_rows, snapshot):
            try:
                rows = refresh_stock_bar_cache(db, stock, snapshot)
                return dynamic_stock_bar_dicts_to_out(rows, snapshot, range_name)
            except Exception:
                db.rollback()
        return dynamic_stock_bars_to_out(cached_rows, snapshot, range_name)
    try:
        rows = refresh_stock_bar_cache(db, stock, snapshot)
        return dynamic_stock_bar_dicts_to_out(rows, snapshot, range_name)
    except Exception as exc:  # noqa: BLE001
        if cached_rows:
            return dynamic_stock_bars_to_out(cached_rows, snapshot, range_name)
        raise ValueError(clean_text(str(exc)) or "历史行情获取失败") from exc


def add_sector_member(db: Session, sector_id: int, query: str, source: str = "manual", source_note: str | None = None) -> dict[str, Any]:
    sector = get_sector(db, sector_id)
    stock = resolve_stock(db, query)
    if not stock:
        raise ValueError("请选择候选股票后添加。")
    existing = db.scalar(
        select(SectorIndexMember).where(
            SectorIndexMember.sector_id == sector.id,
            SectorIndexMember.full_code == stock.full_code,
        )
    )
    if existing:
        existing.source = clean_source(source)
        existing.source_note = clean_optional_text(source_note)
        existing.name = stock.name
        existing.updated_at = now_utc()
        db.commit()
        db.refresh(existing)
        try_recalculate_sector(db, sector.id)
        return sector_member_to_out(db, existing)
    member = SectorIndexMember(
        sector_id=sector.id,
        code=stock.code,
        name=stock.name,
        exchange=stock.exchange,
        full_code=stock.full_code,
        source=clean_source(source),
        source_note=clean_optional_text(source_note),
    )
    db.add(member)
    sector.updated_at = now_utc()
    db.commit()
    db.refresh(member)
    try_recalculate_sector(db, sector.id)
    return sector_member_to_out(db, member)


def delete_sector_member(db: Session, sector_id: int, full_code: str) -> dict[str, str]:
    sector = get_sector(db, sector_id)
    normalized = normalize_full_code(full_code)
    member = db.scalar(
        select(SectorIndexMember).where(
            SectorIndexMember.sector_id == sector.id,
            SectorIndexMember.full_code == normalized,
        )
    )
    if not member:
        raise ValueError("成分股不存在")
    db.delete(member)
    sector.updated_at = now_utc()
    db.commit()
    try_recalculate_sector(db, sector.id)
    return {"status": "ok", "message": "成分股已删除"}


def recalculate_sector_index(db: Session, sector_id: int) -> int:
    sector = get_sector(db, sector_id)
    members = list(
        db.scalars(
            select(SectorIndexMember)
            .where(SectorIndexMember.sector_id == sector.id)
            .order_by(SectorIndexMember.exchange, SectorIndexMember.code)
        )
    )
    if not members:
        db.execute(delete(SectorIndexBar).where(SectorIndexBar.sector_id == sector.id))
        sector.updated_at = now_utc()
        db.commit()
        return 0

    histories: dict[str, dict[date, dict[str, float]]] = {}
    errors: list[str] = []
    for member in members:
        try:
            history = fetch_member_history(member.code)
            if history:
                histories[member.full_code] = history
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{member.name}: {clean_text(str(exc))[:120]}")
    if not histories:
        raise ValueError("板块成分股历史行情不足，暂时无法重算 K 线。" + ("；".join(errors[:3]) if errors else ""))

    common_dates = sorted(set.intersection(*(set(history.keys()) for history in histories.values())))
    if len(common_dates) < 2:
        raise ValueError("板块成分股共同交易日不足，暂时无法重算 K 线。")

    base_date = common_dates[0]
    base_close_by_code: dict[str, float] = {}
    for full_code, history in histories.items():
        base_close = history[base_date]["close"]
        if base_close > 0:
            base_close_by_code[full_code] = base_close
    if not base_close_by_code:
        raise ValueError("板块成分股基准日收盘价无效。")

    active_histories = {
        full_code: history
        for full_code, history in histories.items()
        if full_code in base_close_by_code
    }
    common_dates = [
        trade_date
        for trade_date in common_dates
        if all(trade_date in history for history in active_histories.values())
    ]
    if len(common_dates) < 2:
        raise ValueError("板块成分股共同交易日不足，暂时无法重算 K 线。")

    db.execute(delete(SectorIndexBar).where(SectorIndexBar.sector_id == sector.id))
    previous_close: float | None = None
    count = 0
    for trade_date in common_dates:
        normalized_rows = []
        for full_code, history in active_histories.items():
            row = history[trade_date]
            base_close = base_close_by_code[full_code]
            normalized_rows.append(
                {
                    "open": SECTOR_BASE_VALUE * row["open"] / base_close,
                    "high": SECTOR_BASE_VALUE * row["high"] / base_close,
                    "low": SECTOR_BASE_VALUE * row["low"] / base_close,
                    "close": SECTOR_BASE_VALUE * row["close"] / base_close,
                }
            )
        open_value = average(row["open"] for row in normalized_rows)
        high_value = average(row["high"] for row in normalized_rows)
        low_value = average(row["low"] for row in normalized_rows)
        close_value = average(row["close"] for row in normalized_rows)
        amount_total = sum(
            row["amount"]
            for history in active_histories.values()
            for row in [history[trade_date]]
            if row.get("amount")
        )
        change_pct = round((close_value / previous_close - 1) * 100, 4) if previous_close else None
        previous_close = close_value
        db.add(
            SectorIndexBar(
                sector_id=sector.id,
                trade_date=trade_date,
                open=round(open_value, 4),
                high=round(high_value, 4),
                low=round(low_value, 4),
                close=round(close_value, 4),
                change_pct=change_pct,
                amount=round(amount_total, 2) if amount_total > 0 else None,
                member_count=len(active_histories),
            )
        )
        count += 1
    sector.updated_at = now_utc()
    db.commit()
    return count


def recalculate_sector_result(db: Session, sector_id: int) -> dict[str, Any]:
    count = recalculate_sector_index(db, sector_id)
    sector = get_sector(db, sector_id)
    return {
        "status": "ok",
        "message": f"已重算 {count} 根板块 K 线。",
        "sector": sector_to_summary(db, sector),
        "bar_count": count,
    }


def recognize_sector_image_candidates(
    db: Session,
    sector_id: int,
    image_bytes: bytes,
    content_type: str | None,
) -> dict[str, Any]:
    get_sector(db, sector_id)
    if not image_bytes:
        raise ValueError("图片为空")
    config = image_ocr_config()
    if not config:
        return {"status": "not_configured", "message": "图片识别未配置，可继续手动搜索添加成分股。", "candidates": []}

    mime = content_type or "image/png"
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    prompt = (
        "请从这张股票截图里识别 A 股股票。只返回 JSON，不要解释。"
        "格式：{\"stocks\":[{\"name\":\"股票名称\",\"code\":\"六位代码\"}]}。"
        "如果只有名称或只有代码，也照样返回对应字段；不要编造看不清的股票。"
    )
    try:
        with httpx.Client(timeout=45) as client:
            response = client.post(
                chat_completions_url(config["base_url"]),
                headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
                json={
                    "model": config["model"],
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                            ],
                        }
                    ],
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            payload = response.json()
        choices = payload.get("choices") or []
        raw_text = choices[0].get("message", {}).get("content", "") if choices else ""
        mentions = parse_image_stock_mentions(raw_text)
        candidates = [match_image_candidate(db, sector_id, mention) for mention in mentions]
        unique = dedupe_candidates(candidates)
        status = "ok" if unique else "not_found"
        message = f"识别到 {len(unique)} 个候选，请确认后加入板块。" if unique else "没有识别到可匹配的 A 股候选。"
        return {"status": status, "message": message, "candidates": unique}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"图片识别失败：{clean_text(str(exc))[:200]}", "candidates": []}


def try_recalculate_sector(db: Session, sector_id: int) -> None:
    try:
        recalculate_sector_index(db, sector_id)
    except Exception:
        db.rollback()


def fetch_member_history(code: str) -> dict[date, dict[str, Any]]:
    end = datetime.now(BEIJING).date()
    days = max(120, int(os.environ.get("SECTOR_INDEX_HISTORY_DAYS", str(DEFAULT_HISTORY_DAYS))))
    start = end - timedelta(days=days)
    start_text = start.strftime("%Y%m%d")
    end_text = end.strftime("%Y%m%d")
    errors: list[str] = []
    price_fallback: dict[date, dict[str, Any]] | None = None
    for source in member_history_source_order():
        try:
            frame = fetch_history_frame(source, code, start_text, end_text)
            history = parse_history_frame(frame, source)
            if history:
                if any(row.get("amount") for row in history.values()):
                    return history
                if price_fallback is None:
                    price_fallback = history
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}: {clean_text(str(exc))[:80]}")
    if price_fallback:
        return price_fallback
    raise ValueError("；".join(errors) or "历史行情获取失败")


def member_history_source_order() -> list[str]:
    sources = history_source_order()
    ordered = [source for source in ("sina", "eastmoney", "tencent") if source in sources]
    return ordered or sources


def parse_history_frame(frame: Any, source: str | None = None) -> dict[date, dict[str, Any]]:
    if frame is None or getattr(frame, "empty", False):
        return {}
    records = frame.to_dict("records")
    rows: dict[date, dict[str, float]] = {}
    for record in records:
        trade_date = parse_date(first_value(record, "日期", "date"))
        open_value = parse_float(first_value(record, "开盘", "开盘价", "open"))
        high_value = parse_float(first_value(record, "最高", "最高价", "high"))
        low_value = parse_float(first_value(record, "最低", "最低价", "low"))
        close_value = parse_float(first_value(record, "收盘", "收盘价", "close"))
        amount_value = parse_history_amount(record, source)
        if not trade_date or not open_value or not high_value or not low_value or not close_value:
            continue
        if min(open_value, high_value, low_value, close_value) <= 0:
            continue
        rows[trade_date] = {
            "open": open_value,
            "high": high_value,
            "low": low_value,
            "close": close_value,
            "source": source or "unknown",
        }
        if amount_value:
            rows[trade_date]["amount"] = amount_value
    return dict(sorted(rows.items(), key=lambda item: item[0]))


def parse_history_amount(record: dict[str, Any], source: str | None = None) -> float | None:
    source_key = (source or "").lower()
    aliases = ("成交额", "成交金额", "成交额(元)", "成交金额(元)")
    if source_key != "tencent":
        aliases = (*aliases, "amount")
    amount = parse_float(first_value(record, *aliases))
    if amount is None or amount <= 0:
        return None
    return amount


def range_start_date(range_name: str) -> date | None:
    range_key = (range_name or "6m").lower()
    if range_key == "all":
        return None
    days = BAR_RANGE_DAYS.get(range_key, BAR_RANGE_DAYS["6m"])
    return datetime.now(BEIJING).date() - timedelta(days=days)


def stock_history_rows(history: dict[date, dict[str, Any]]) -> list[dict[str, Any]]:
    previous_close: float | None = None
    result: list[dict[str, Any]] = []
    for trade_date, row in sorted(history.items(), key=lambda item: item[0]):
        close_value = row["close"]
        change_pct = round((close_value / previous_close - 1) * 100, 4) if previous_close else None
        previous_close = close_value
        result.append(
            {
                "trade_date": trade_date,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": close_value,
                "change_pct": change_pct,
                "amount": row.get("amount"),
                "source": row.get("source"),
            }
        )
    return result


def stock_bar_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": row["trade_date"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "change_pct": row.get("change_pct"),
        "amount": row.get("amount"),
    }


def filter_stock_bar_dicts(rows: list[dict[str, Any]], range_name: str) -> list[dict[str, Any]]:
    start = range_start_date(range_name)
    filtered = rows if start is None else [row for row in rows if row["trade_date"] >= start]
    return [stock_bar_public(row) for row in filtered]


def cached_stock_bar_rows(db: Session, full_code: str) -> list[StockDailyBar]:
    return list(
        db.scalars(
            select(StockDailyBar)
            .where(StockDailyBar.full_code == full_code)
            .order_by(StockDailyBar.trade_date)
        )
    )


def stock_bar_cache_is_fresh(rows: list[StockDailyBar]) -> bool:
    latest = max((row.fetched_at for row in rows if row.fetched_at), default=None)
    if not latest:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest.astimezone(BEIJING).date() == datetime.now(BEIJING).date()


def is_after_post_close_refresh_time(now: datetime | None = None) -> bool:
    current = now or datetime.now(BEIJING)
    hour, minute = POST_CLOSE_REFRESH_TIME
    return (current.hour, current.minute) >= (hour, minute)


def stock_bar_cache_has_post_close_fetch(rows: list[StockDailyBar]) -> bool:
    latest = max((row.fetched_at for row in rows if row.fetched_at), default=None)
    if not latest:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    local_latest = latest.astimezone(BEIJING)
    hour, minute = POST_CLOSE_REFRESH_TIME
    return local_latest.date() == datetime.now(BEIJING).date() and (local_latest.hour, local_latest.minute) >= (hour, minute)


def should_refresh_stock_bar_cache_after_close(rows: list[StockDailyBar], snapshot: FactorQuoteSnapshot | None) -> bool:
    if not is_after_post_close_refresh_time():
        return False
    snapshot_date = snapshot.trade_date if snapshot else None
    latest_row = latest_stock_bar_row(rows)
    latest_bar_date = latest_row.trade_date if latest_row else None
    if snapshot_date and (latest_bar_date is None or latest_bar_date < snapshot_date):
        return True
    if snapshot_date and latest_bar_date == snapshot_date and latest_row and latest_row.source == "snapshot_post_close":
        return not stock_bar_cache_was_fetched_recently(rows, STOCK_BAR_POST_CLOSE_RETRY_SECONDS)
    return not stock_bar_cache_has_post_close_fetch(rows)


def latest_stock_bar_row(rows: list[StockDailyBar]) -> StockDailyBar | None:
    return max(rows, key=lambda row: row.trade_date, default=None)


def stock_bar_cache_was_fetched_recently(rows: list[StockDailyBar], seconds: int) -> bool:
    latest = max((row.fetched_at for row in rows if row.fetched_at), default=None)
    if not latest:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age = now_utc() - latest.astimezone(timezone.utc)
    return age.total_seconds() < seconds


def refresh_stock_bar_cache(
    db: Session,
    stock: AStock,
    snapshot: FactorQuoteSnapshot | None = None,
) -> list[dict[str, Any]]:
    history = fetch_member_history(stock.code)
    rows = stock_history_rows(history)
    rows = fill_post_close_snapshot_bar(rows, snapshot)
    if not rows:
        raise ValueError("历史行情为空")
    replace_stock_bar_cache(db, stock, rows)
    return rows


def fill_post_close_snapshot_bar(
    rows: list[dict[str, Any]],
    snapshot: FactorQuoteSnapshot | None,
) -> list[dict[str, Any]]:
    if not is_after_post_close_refresh_time():
        return rows
    fallback = snapshot_stock_bar(snapshot)
    if not fallback:
        return rows
    if rows and rows[-1]["trade_date"] >= fallback["trade_date"]:
        return rows
    return [*rows, fallback]


def snapshot_stock_bar(snapshot: FactorQuoteSnapshot | None) -> dict[str, Any] | None:
    if not snapshot or not snapshot.trade_date or snapshot.latest_price is None or snapshot.change_pct is None:
        return None
    close_value = float(snapshot.latest_price)
    change_pct = float(snapshot.change_pct)
    denominator = 1 + change_pct / 100
    if close_value <= 0 or abs(denominator) < 0.000001:
        return None
    open_value = close_value / denominator
    return {
        "trade_date": snapshot.trade_date,
        "open": round(open_value, 4),
        "high": round(max(open_value, close_value), 4),
        "low": round(min(open_value, close_value), 4),
        "close": round(close_value, 4),
        "change_pct": round(change_pct, 4),
        "amount": None,
        "source": "snapshot_post_close",
    }


def schedule_stock_bar_cache_refresh(full_code: str) -> None:
    with _STOCK_BAR_REFRESH_LOCK:
        if full_code in _STOCK_BAR_REFRESHING:
            return
        _STOCK_BAR_REFRESHING.add(full_code)
    worker = threading.Timer(
        STOCK_BAR_BACKGROUND_REFRESH_DELAY_SECONDS,
        refresh_stock_bar_cache_background,
        args=(full_code,),
    )
    worker.name = f"stock-bars-refresh-{full_code}"
    worker.daemon = True
    worker.start()


def refresh_stock_bar_cache_background(full_code: str) -> None:
    db = SessionLocal()
    try:
        stock = db.scalar(select(AStock).where(AStock.full_code == full_code))
        if stock:
            snapshot = db.scalar(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code == full_code))
            refresh_stock_bar_cache(db, stock, snapshot)
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()
        with _STOCK_BAR_REFRESH_LOCK:
            _STOCK_BAR_REFRESHING.discard(full_code)


def filter_cached_stock_rows(rows: list[StockDailyBar], range_name: str) -> list[StockDailyBar]:
    start = range_start_date(range_name)
    return rows if start is None else [row for row in rows if row.trade_date >= start]


def cached_stock_bars_to_out(rows: list[StockDailyBar]) -> list[dict[str, Any]]:
    return [
        {
            "trade_date": row.trade_date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "change_pct": row.change_pct,
            "amount": row.amount,
        }
        for row in rows
    ]


def stock_bar_dicts_from_cache(rows: list[StockDailyBar]) -> list[dict[str, Any]]:
    return [
        {
            "trade_date": row.trade_date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "change_pct": row.change_pct,
            "amount": row.amount,
        }
        for row in rows
    ]


def dynamic_stock_bars_to_out(rows: list[StockDailyBar], snapshot: FactorQuoteSnapshot | None, range_name: str) -> list[dict[str, Any]]:
    return dynamic_stock_bar_dicts_to_out(stock_bar_dicts_from_cache(rows), snapshot, range_name)


def dynamic_stock_bar_dicts_to_out(rows: list[dict[str, Any]], snapshot: FactorQuoteSnapshot | None, range_name: str) -> list[dict[str, Any]]:
    dynamic_rows = apply_intraday_stock_bar(rows, snapshot)
    return filter_stock_bar_dicts(dynamic_rows, range_name)


def apply_intraday_stock_bar(rows: list[dict[str, Any]], snapshot: FactorQuoteSnapshot | None) -> list[dict[str, Any]]:
    if not rows or not snapshot or not snapshot.trade_date or snapshot.latest_price is None or snapshot.change_pct is None:
        return rows
    latest_date = rows[-1]["trade_date"]
    if is_after_post_close_refresh_time() and latest_date >= snapshot.trade_date:
        return rows
    close_value = float(snapshot.latest_price)
    change_pct = float(snapshot.change_pct)
    denominator = 1 + change_pct / 100
    if close_value <= 0 or abs(denominator) < 0.000001:
        return rows
    open_value = close_value / denominator
    dynamic_row = {
        "trade_date": snapshot.trade_date,
        "open": round(open_value, 4),
        "high": round(max(open_value, close_value), 4),
        "low": round(min(open_value, close_value), 4),
        "close": round(close_value, 4),
        "change_pct": round(change_pct, 4),
        "amount": None,
    }
    result = [dict(row) for row in rows]
    latest_index = len(result) - 1
    if latest_date == snapshot.trade_date:
        previous = result[latest_index]
        result[latest_index] = {
            **previous,
            **dynamic_row,
            "high": round(max(previous.get("high") or dynamic_row["high"], dynamic_row["high"]), 4),
            "low": round(min(previous.get("low") or dynamic_row["low"], dynamic_row["low"]), 4),
            "amount": previous.get("amount"),
        }
    elif latest_date < snapshot.trade_date:
        result.append(dynamic_row)
    return result


def replace_stock_bar_cache(db: Session, stock: AStock, rows: list[dict[str, Any]]) -> None:
    fetched_at = now_utc()
    db.execute(delete(StockDailyBar).where(StockDailyBar.full_code == stock.full_code))
    for row in rows:
        db.add(
            StockDailyBar(
                code=stock.code,
                name=stock.name,
                exchange=stock.exchange,
                full_code=stock.full_code,
                trade_date=row["trade_date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                change_pct=row.get("change_pct"),
                amount=row.get("amount"),
                source=row.get("source") or "unknown",
                fetched_at=fetched_at,
            )
        )
    db.commit()


def get_sector(db: Session, sector_id: int) -> SectorIndex:
    ensure_sector_group_columns(db)
    sector = db.get(SectorIndex, sector_id)
    if not sector or not sector.active:
        raise ValueError("板块不存在")
    return sector


def empty_sector_stats() -> dict[str, int]:
    return {
        "member_count": 0,
        "quoted_member_count": 0,
        "rising_member_count": 0,
        "falling_member_count": 0,
        "latest_change_pct": None,
        "latest_trade_date": None,
        "updated_at": None,
    }


def ensure_sector_group_columns(db: Session) -> None:
    columns = {row[1] for row in db.execute(text("pragma table_info(sector_indices)")).all()}
    statements = []
    if "parent_name" not in columns:
        statements.append("alter table sector_indices add column parent_name varchar(80)")
    if "group_sort_order" not in columns:
        statements.append("alter table sector_indices add column group_sort_order integer not null default 100")
    if "source_image" not in columns:
        statements.append("alter table sector_indices add column source_image text")
    for statement in statements:
        db.execute(text(statement))
    if statements:
        db.commit()


def grouped_sector_summaries(sectors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for sector in sectors:
        group_name = clean_text(sector.get("parent_name") or "未分组") or "未分组"
        group = grouped.setdefault(
            group_name,
            {
                "name": group_name,
                "sort_order": int(sector.get("group_sort_order") or 100),
                "sector_count": 0,
                "member_count": 0,
                "sectors": [],
            },
        )
        group["sort_order"] = min(group["sort_order"], int(sector.get("group_sort_order") or 100))
        group["sector_count"] += 1
        group["member_count"] += int(sector.get("member_count") or 0)
        group["sectors"].append(sector)
    return sorted(grouped.values(), key=lambda row: (row["sort_order"], row["name"]))


def sector_summary_maps(db: Session, sector_ids: list[int]) -> tuple[dict[int, dict[str, int]], dict[int, SectorIndexBar]]:
    if not sector_ids:
        return {}, {}
    stats_rows = db.execute(
        select(
            SectorIndexMember.sector_id,
            func.count(SectorIndexMember.id).label("member_count"),
            func.count(FactorQuoteSnapshot.full_code).label("quoted_member_count"),
            func.coalesce(func.sum(case((FactorQuoteSnapshot.change_pct > 0, 1), else_=0)), 0).label("rising_member_count"),
            func.coalesce(func.sum(case((FactorQuoteSnapshot.change_pct < 0, 1), else_=0)), 0).label("falling_member_count"),
            func.avg(FactorQuoteSnapshot.change_pct).label("latest_change_pct"),
            func.max(FactorQuoteSnapshot.trade_date).label("latest_trade_date"),
            func.max(FactorQuoteSnapshot.updated_at).label("updated_at"),
        )
        .outerjoin(FactorQuoteSnapshot, FactorQuoteSnapshot.full_code == SectorIndexMember.full_code)
        .where(SectorIndexMember.sector_id.in_(sector_ids))
        .group_by(SectorIndexMember.sector_id)
    ).all()
    stats_map = {
        row.sector_id: {
            "member_count": int(row.member_count or 0),
            "quoted_member_count": int(row.quoted_member_count or 0),
            "rising_member_count": int(row.rising_member_count or 0),
            "falling_member_count": int(row.falling_member_count or 0),
            "latest_change_pct": round(float(row.latest_change_pct), 4) if row.latest_change_pct is not None else None,
            "latest_trade_date": row.latest_trade_date,
            "updated_at": row.updated_at,
        }
        for row in stats_rows
    }
    latest_dates = (
        select(
            SectorIndexBar.sector_id.label("sector_id"),
            func.max(SectorIndexBar.trade_date).label("trade_date"),
        )
        .where(SectorIndexBar.sector_id.in_(sector_ids))
        .group_by(SectorIndexBar.sector_id)
        .subquery()
    )
    latest_rows = db.scalars(
        select(SectorIndexBar).join(
            latest_dates,
            and_(
                SectorIndexBar.sector_id == latest_dates.c.sector_id,
                SectorIndexBar.trade_date == latest_dates.c.trade_date,
            ),
        )
    ).all()
    latest_bar_map = {row.sector_id: row for row in latest_rows}
    return stats_map, latest_bar_map


def dynamic_sector_close(
    latest_bar: SectorIndexBar | dict[str, Any] | None,
    latest_change_pct: float | None,
    latest_trade_date: date | None,
) -> float | None:
    if latest_bar is None or latest_change_pct is None:
        return None
    close = latest_bar["close"] if isinstance(latest_bar, dict) else latest_bar.close
    bar_date = latest_bar["trade_date"] if isinstance(latest_bar, dict) else latest_bar.trade_date
    bar_change_pct = latest_bar.get("change_pct") if isinstance(latest_bar, dict) else latest_bar.change_pct
    base_close = close
    if latest_trade_date == bar_date and bar_change_pct is not None:
        denominator = 1 + bar_change_pct / 100
        if abs(denominator) > 0.000001:
            base_close = close / denominator
    return round(base_close * (1 + latest_change_pct / 100), 4)


def sector_bars_to_out(db: Session, sector_id: int, rows: list[SectorIndexBar]) -> list[dict[str, Any]]:
    payload = bars_to_out(rows)
    if not payload:
        return payload
    stats_map, _latest_bar_map = sector_summary_maps(db, [sector_id])
    stats = stats_map.get(sector_id, empty_sector_stats())
    latest_change_pct = stats.get("latest_change_pct")
    latest_trade_date = stats.get("latest_trade_date")
    if latest_change_pct is None or latest_trade_date is None:
        return payload
    latest_row = payload[-1]
    dynamic_close = dynamic_sector_close(latest_row, latest_change_pct, latest_trade_date)
    if dynamic_close is None:
        return payload
    updated_at = stats.get("updated_at") or latest_row["created_at"]
    if latest_trade_date == latest_row["trade_date"]:
        payload[-1] = {
            **latest_row,
            "high": max(latest_row["high"], dynamic_close),
            "low": min(latest_row["low"], dynamic_close),
            "close": dynamic_close,
            "change_pct": latest_change_pct,
            "member_count": stats["quoted_member_count"] or stats["member_count"] or latest_row["member_count"],
            "created_at": updated_at,
        }
    elif latest_trade_date > latest_row["trade_date"]:
        previous_close = latest_row["close"]
        payload.append(
            {
                "trade_date": latest_trade_date,
                "open": previous_close,
                "high": max(previous_close, dynamic_close),
                "low": min(previous_close, dynamic_close),
                "close": dynamic_close,
                "change_pct": latest_change_pct,
                "amount": None,
                "member_count": stats["quoted_member_count"] or stats["member_count"],
                "created_at": updated_at,
            }
        )
    return payload


def sector_to_summary(
    db: Session,
    sector: SectorIndex,
    stats: dict[str, int] | None = None,
    latest_bar: SectorIndexBar | None | object = _MISSING,
) -> dict[str, Any]:
    if stats is None:
        stats_map, latest_bar_map = sector_summary_maps(db, [sector.id])
        stats = stats_map.get(sector.id, empty_sector_stats())
        if latest_bar is _MISSING:
            latest_bar = latest_bar_map.get(sector.id)
    elif latest_bar is _MISSING:
        latest_bar = None
    pinyin = "".join(lazy_pinyin(sector.name)).lower()
    stats_trade_date = stats.get("latest_trade_date")
    use_quote_stats = bool(latest_bar and stats_trade_date and stats_trade_date >= latest_bar.trade_date)
    dynamic_close = (
        dynamic_sector_close(latest_bar, stats.get("latest_change_pct"), stats_trade_date)
        if latest_bar and use_quote_stats
        else None
    )
    return {
        "id": sector.id,
        "name": sector.name,
        "description": sector.description,
        "parent_name": sector.parent_name,
        "group_sort_order": sector.group_sort_order,
        "source_image": sector.source_image,
        "pinyin": pinyin,
        "pinyin_initials": pinyin_initials(sector.name),
        "active": sector.active,
        "sort_order": sector.sort_order,
        "member_count": stats["member_count"],
        "rising_member_count": stats["rising_member_count"],
        "falling_member_count": stats["falling_member_count"],
        "quoted_member_count": stats["quoted_member_count"],
        "latest_close": dynamic_close if dynamic_close is not None else latest_bar.close if latest_bar else None,
        "latest_change_pct": (
            stats.get("latest_change_pct")
            if use_quote_stats and stats.get("latest_change_pct") is not None
            else latest_bar.change_pct if latest_bar else None
        ),
        "latest_trade_date": stats_trade_date if use_quote_stats else (latest_bar.trade_date if latest_bar else stats_trade_date),
        "updated_at": stats.get("updated_at") if use_quote_stats and stats.get("updated_at") else (latest_bar.created_at if latest_bar else sector.updated_at),
    }


def sector_members_to_out(db: Session, sector_id: int) -> list[dict[str, Any]]:
    rows = list(
        db.execute(
            select(SectorIndexMember, FactorQuoteSnapshot)
            .outerjoin(FactorQuoteSnapshot, FactorQuoteSnapshot.full_code == SectorIndexMember.full_code)
            .where(SectorIndexMember.sector_id == sector_id)
            .order_by(SectorIndexMember.exchange, SectorIndexMember.code)
        )
    )
    full_codes = [member.full_code for member, _snapshot in rows]
    tag_map = tags_for_full_codes(db, full_codes)
    status_map = review_statuses_for_full_codes(db, full_codes, tag_map)
    return [
        sector_member_to_out(
            db,
            member,
            snapshot,
            factor_tags=tag_map.get(member.full_code, []),
            factor_tag_status=status_map.get(member.full_code),
        )
        for member, snapshot in rows
    ]


def sector_member_to_out(
    db: Session,
    member: SectorIndexMember,
    snapshot: FactorQuoteSnapshot | None | object = _MISSING,
    factor_tags: list[Any] | None = None,
    factor_tag_status: str | None = None,
) -> dict[str, Any]:
    if snapshot is _MISSING:
        snapshot = db.scalar(select(FactorQuoteSnapshot).where(FactorQuoteSnapshot.full_code == member.full_code))
    if factor_tags is None:
        factor_tags = tags_for_full_codes(db, [member.full_code]).get(member.full_code, [])
    if factor_tag_status is None:
        factor_tag_status = review_statuses_for_full_codes(db, [member.full_code], {member.full_code: factor_tags}).get(
            member.full_code,
            inferred_review_status(None, bool(factor_tags)),
        )
    return {
        "id": member.id,
        "sector_id": member.sector_id,
        "code": member.code,
        "name": member.name,
        "exchange": member.exchange,
        "full_code": member.full_code,
        "source": member.source,
        "source_note": member.source_note,
        "latest_price": snapshot.latest_price if snapshot else None,
        "change_pct": snapshot.change_pct if snapshot else None,
        "factor_tags": [tag_to_plain(tag) for tag in factor_tags],
        "factor_tag_status": factor_tag_status,
        "created_at": member.created_at,
        "updated_at": member.updated_at,
    }


def recent_sector_bars(db: Session, sector_id: int, limit: int) -> list[SectorIndexBar]:
    rows = list(
        db.scalars(
            select(SectorIndexBar)
            .where(SectorIndexBar.sector_id == sector_id)
            .order_by(desc(SectorIndexBar.trade_date))
            .limit(limit)
        )
    )
    return list(reversed(rows))


def bars_to_out(rows: list[SectorIndexBar]) -> list[dict[str, Any]]:
    return [
        {
            "trade_date": row.trade_date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "change_pct": row.change_pct,
            "amount": row.amount,
            "member_count": row.member_count,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def stock_history_to_out(history: dict[date, dict[str, Any]]) -> list[dict[str, Any]]:
    return [stock_bar_public(row) for row in stock_history_rows(history)]


def resolve_stock(db: Session, query: str) -> AStock | None:
    ensure_stock_universe(db)
    text = clean_text(query).upper().replace(" ", "")
    if not text:
        return None
    full_code = normalize_full_code(text)
    if full_code:
        stock = db.scalar(select(AStock).where(AStock.full_code == full_code))
        if stock:
            return stock
    code = normalize_code(text)
    if code:
        stock = db.scalar(select(AStock).where(AStock.code == code))
        if stock:
            return stock
    stock = db.scalar(select(AStock).where(func.upper(AStock.name) == text))
    if stock:
        return stock
    candidates = search_a_stocks(db, query, limit=5)
    if not candidates:
        return None
    return db.scalar(select(AStock).where(AStock.full_code == candidates[0]["full_code"]))


def match_image_candidate(db: Session, sector_id: int, mention: dict[str, str]) -> dict[str, Any]:
    code = normalize_code(mention.get("code"))
    name = clean_text(mention.get("name"))
    source_text = clean_text(mention.get("source_text") or f"{name} {code or ''}") or "未命名候选"
    stock = None
    if code:
        stock = db.scalar(select(AStock).where(AStock.code == code))
    if not stock and name:
        stock = resolve_stock(db, name)
    if not stock and code:
        stock = resolve_stock(db, code)
    if not stock:
        return {
            "source_text": source_text,
            "code": code,
            "name": name or None,
            "exchange": None,
            "full_code": None,
            "matched": False,
            "exists": False,
            "message": "未匹配到本地 A 股",
        }
    exists = db.scalar(
        select(SectorIndexMember.id).where(
            SectorIndexMember.sector_id == sector_id,
            SectorIndexMember.full_code == stock.full_code,
        )
    )
    return {
        "source_text": source_text,
        "code": stock.code,
        "name": stock.name,
        "exchange": stock.exchange,
        "full_code": stock.full_code,
        "matched": True,
        "exists": bool(exists),
        "message": "已在板块中" if exists else None,
    }


def parse_image_stock_mentions(raw_text: str) -> list[dict[str, str]]:
    text = clean_json_text(raw_text)
    mentions: list[dict[str, str]] = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            rows = parsed.get("stocks") or parsed.get("items") or parsed.get("data") or []
        else:
            rows = parsed
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code = normalize_code(first_value(row, "code", "股票代码", "代码") or "")
                name = clean_text(first_value(row, "name", "股票名称", "名称", "简称") or "")
                if code or name:
                    mentions.append({"code": code or "", "name": name, "source_text": clean_text(f"{name} {code or ''}")})
    except json.JSONDecodeError:
        pass
    if mentions:
        return mentions[:80]

    seen_codes: set[str] = set()
    for match in re.finditer(r"(?<!\d)(\d{6})(?!\d)", raw_text):
        code = match.group(1)
        if code in seen_codes:
            continue
        seen_codes.add(code)
        mentions.append({"code": code, "name": "", "source_text": code})
    return mentions[:80]


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        key = candidate.get("full_code") or f"{candidate.get('source_text')}:{candidate.get('code')}"
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def normalize_full_code(value: Any) -> str:
    text = clean_text(value).upper().replace(".", "").replace("-", "")
    code = normalize_code(text)
    if not code:
        return ""
    prefix = text[:2] if text[:2] in {"SH", "SZ", "BJ"} else exchange_from_code(code)
    return f"{prefix}{code}"


def clean_sector_name(value: Any) -> str:
    name = clean_text(value)
    if not name:
        raise ValueError("板块名称不能为空")
    if len(name) > 80:
        raise ValueError("板块名称过长")
    return name


def clean_optional_text(value: Any) -> str | None:
    text = clean_text(value)
    return text or None


def clean_source(value: Any) -> str:
    source = clean_text(value) or "manual"
    return source[:32]


def average(values: Any) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0


def pinyin_initials(value: str) -> str:
    return "".join(part[:1] for part in lazy_pinyin(value) if part).lower()


def image_ocr_config() -> dict[str, str] | None:
    enabled = (config_value("NETWORK_IMAGE_OCR_ENABLED") or "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return None
    api_key = config_value("NETWORK_IMAGE_OCR_API_KEY") or config_value("OPENAI_API_KEY")
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "model": config_value("NETWORK_IMAGE_OCR_MODEL") or config_value("OPENAI_VISION_MODEL") or "gpt-4o-mini",
        "base_url": config_value("NETWORK_IMAGE_OCR_BASE_URL") or config_value("OPENAI_BASE_URL") or "https://api.openai.com/v1",
    }


def config_value(key: str) -> str | None:
    value = os.environ.get(key)
    if value:
        return value.strip()
    env_path = Path(__file__).resolve().parents[2] / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, raw = stripped.split("=", 1)
            if name.strip() == key:
                return raw.strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


def chat_completions_url(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def clean_json_text(value: str) -> str:
    text = clean_text(value)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    start_object = text.find("{")
    end_object = text.rfind("}")
    start_array = text.find("[")
    end_array = text.rfind("]")
    if start_object >= 0 and end_object > start_object:
        return text[start_object : end_object + 1]
    if start_array >= 0 and end_array > start_array:
        return text[start_array : end_array + 1]
    return text
