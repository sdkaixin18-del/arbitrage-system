from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MarketOpportunityGroup, MarketOpportunityItem, now_utc

VERIFICATION_STATUSES = ["待验证", "部分验证", "已验证", "证伪"]
DEFAULT_GROUP_COLOR = "#1d4ed8"
EXAMPLE_SOURCE_NOTE = "示例数据，仅用于展示，请替换"


def ensure_opportunity_map_seed(db: Session) -> None:
    existing = db.scalar(select(func.count(MarketOpportunityGroup.id))) or 0
    if existing:
        return
    today = date.today()
    group_a = MarketOpportunityGroup(
        name="示例：高端材料国产替代",
        subtitle="示例方向，请替换为真实产业判断",
        color="#2563eb",
        sort_order=10,
    )
    group_b = MarketOpportunityGroup(
        name="示例：设备验证放量",
        subtitle="示例方向，请替换为真实产业判断",
        color="#16a34a",
        sort_order=20,
    )
    db.add_all([group_a, group_b])
    db.flush()
    db.add_all(
        [
            MarketOpportunityItem(
                group_id=group_a.id,
                company_name="示例公司A",
                stock_code="000000",
                feature_title="示例：弹性标的",
                feature_tags_json=json_list(["示例标签", "需替换"]),
                feature_desc="这里填写弹性特征、产业链定位和跟踪口径。",
                order_checks_json=json_list(["示例：订单/客户验证点", "示例：产能释放验证点"]),
                replacement_space="这里填写替代空间、长期空间或渗透率假设。",
                barriers_json=json_list(["示例：工艺壁垒", "示例：客户认证周期"]),
                highlight_level=2,
                verification_status="待验证",
                source_note=EXAMPLE_SOURCE_NOTE,
                data_date=today,
                sort_order=10,
            ),
            MarketOpportunityItem(
                group_id=group_a.id,
                company_name="示例公司B",
                stock_code=None,
                feature_title="示例：均衡型",
                feature_tags_json=json_list(["示例标签"]),
                feature_desc="这里填写公司定位，不代表真实信息。",
                order_checks_json=json_list(["示例：跟踪公告/调研/订单变化"]),
                replacement_space="这里填写替代空间展望。",
                barriers_json=json_list(["示例：供应链资源"]),
                highlight_level=1,
                verification_status="部分验证",
                source_note=EXAMPLE_SOURCE_NOTE,
                data_date=today,
                sort_order=20,
            ),
            MarketOpportunityItem(
                group_id=group_b.id,
                company_name="示例公司C",
                stock_code="111111",
                feature_title="示例：验证阶段",
                feature_tags_json=json_list(["示例标签", "待复核"]),
                feature_desc="这里填写设备验证节点和放量条件。",
                order_checks_json=json_list(["示例：客户验证进度", "示例：订单转化信号"]),
                replacement_space="这里填写潜在替代空间，不代表真实信息。",
                barriers_json=json_list(["示例：认证壁垒", "示例：成本曲线"]),
                highlight_level=0,
                verification_status="待验证",
                source_note=EXAMPLE_SOURCE_NOTE,
                data_date=today,
                sort_order=10,
            ),
        ]
    )
    db.commit()


def opportunity_map_overview(db: Session) -> dict[str, Any]:
    groups = list(
        db.scalars(
            select(MarketOpportunityGroup)
            .where(MarketOpportunityGroup.is_active.is_(True))
            .order_by(MarketOpportunityGroup.sort_order, MarketOpportunityGroup.id)
        )
    )
    group_ids = [group.id for group in groups]
    items = list(
        db.scalars(
            select(MarketOpportunityItem)
            .where(MarketOpportunityItem.group_id.in_(group_ids) if group_ids else False)
            .order_by(MarketOpportunityItem.sort_order, MarketOpportunityItem.id)
        )
    )
    items_by_group: dict[int, list[MarketOpportunityItem]] = {}
    for item in items:
        items_by_group.setdefault(item.group_id, []).append(item)
    payload_groups = [group_to_out(group, items_by_group.get(group.id, [])) for group in groups]
    updated_at = max(
        [group.updated_at for group in groups] + [item.updated_at for item in items],
        default=None,
    )
    return {
        "status": "ok" if groups else "empty",
        "updated_at": updated_at,
        "message": f"已维护 {len(groups)} 个产业方向、{len(items)} 条公司机会。",
        "verification_statuses": VERIFICATION_STATUSES,
        "groups": payload_groups,
    }


def create_opportunity_group(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    group = MarketOpportunityGroup(
        name=clean_required(payload.get("name"), "方向名称不能为空", 80),
        subtitle=clean_optional(payload.get("subtitle"), 160),
        color=clean_color(payload.get("color")),
        sort_order=int(payload.get("sort_order") or 100),
        is_active=True,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group_to_out(group, [])


def update_opportunity_group(db: Session, group_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    group = get_group(db, group_id, include_inactive=True)
    if "name" in payload and payload["name"] is not None:
        group.name = clean_required(payload["name"], "方向名称不能为空", 80)
    if "subtitle" in payload:
        group.subtitle = clean_optional(payload.get("subtitle"), 160)
    if "color" in payload:
        group.color = clean_color(payload.get("color"))
    if "sort_order" in payload and payload["sort_order"] is not None:
        group.sort_order = int(payload["sort_order"])
    if "is_active" in payload and payload["is_active"] is not None:
        group.is_active = bool(payload["is_active"])
    group.updated_at = now_utc()
    db.commit()
    db.refresh(group)
    items = list(
        db.scalars(
            select(MarketOpportunityItem)
            .where(MarketOpportunityItem.group_id == group.id)
            .order_by(MarketOpportunityItem.sort_order, MarketOpportunityItem.id)
        )
    )
    return group_to_out(group, items)


def delete_opportunity_group(db: Session, group_id: int) -> dict[str, str]:
    group = get_group(db, group_id)
    group.is_active = False
    group.updated_at = now_utc()
    db.commit()
    return {"status": "ok", "message": "方向已隐藏"}


def create_opportunity_item(db: Session, group_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    group = get_group(db, group_id)
    row = MarketOpportunityItem(group_id=group.id)
    apply_item_payload(row, payload, creating=True)
    group.updated_at = now_utc()
    db.add(row)
    db.commit()
    db.refresh(row)
    return item_to_out(row)


def update_opportunity_item(db: Session, item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = db.get(MarketOpportunityItem, item_id)
    if not row:
        raise ValueError("公司机会不存在")
    if "group_id" in payload and payload["group_id"] is not None:
        get_group(db, int(payload["group_id"]))
    apply_item_payload(row, payload, creating=False)
    row.updated_at = now_utc()
    group = db.get(MarketOpportunityGroup, row.group_id)
    if group:
        group.updated_at = now_utc()
    db.commit()
    db.refresh(row)
    return item_to_out(row)


def delete_opportunity_item(db: Session, item_id: int) -> dict[str, str]:
    row = db.get(MarketOpportunityItem, item_id)
    if not row:
        raise ValueError("公司机会不存在")
    group = db.get(MarketOpportunityGroup, row.group_id)
    db.delete(row)
    if group:
        group.updated_at = now_utc()
    db.commit()
    return {"status": "ok", "message": "公司机会已删除"}


def apply_item_payload(row: MarketOpportunityItem, payload: dict[str, Any], creating: bool) -> None:
    if "group_id" in payload and payload["group_id"] is not None:
        row.group_id = int(payload["group_id"])
    if creating or "company_name" in payload:
        row.company_name = clean_required(payload.get("company_name"), "公司名称不能为空", 120)
    if creating or "stock_code" in payload:
        row.stock_code = clean_stock_code(payload.get("stock_code"))
    if creating or "feature_title" in payload:
        row.feature_title = clean_optional(payload.get("feature_title"), 160)
    if creating or "feature_tags" in payload:
        row.feature_tags_json = json_list(clean_string_list(payload.get("feature_tags") or []))
    if creating or "feature_desc" in payload:
        row.feature_desc = clean_optional(payload.get("feature_desc"), 4000)
    if creating or "order_checks" in payload:
        row.order_checks_json = json_list(clean_string_list(payload.get("order_checks") or []))
    if creating or "replacement_space" in payload:
        row.replacement_space = clean_optional(payload.get("replacement_space"), 4000)
    if creating or "barriers" in payload:
        row.barriers_json = json_list(clean_string_list(payload.get("barriers") or []))
    if creating or "highlight_level" in payload:
        row.highlight_level = max(0, min(2, int(payload.get("highlight_level") or 0)))
    if creating or "verification_status" in payload:
        row.verification_status = clean_status(payload.get("verification_status"))
    if creating or "source_note" in payload:
        row.source_note = clean_required(payload.get("source_note"), "来源说明不能为空", 1000)
    if creating or "data_date" in payload:
        value = payload.get("data_date")
        if not value:
            raise ValueError("数据日期不能为空")
        row.data_date = value
    if creating or "sort_order" in payload:
        row.sort_order = int(payload.get("sort_order") or 100)


def group_to_out(group: MarketOpportunityGroup, items: list[MarketOpportunityItem]) -> dict[str, Any]:
    return {
        "id": group.id,
        "name": group.name,
        "subtitle": group.subtitle,
        "color": group.color,
        "sort_order": group.sort_order,
        "is_active": group.is_active,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
        "items": [item_to_out(item) for item in items],
    }


def item_to_out(item: MarketOpportunityItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "group_id": item.group_id,
        "company_name": item.company_name,
        "stock_code": item.stock_code,
        "feature_title": item.feature_title,
        "feature_tags": json_load_list(item.feature_tags_json),
        "feature_desc": item.feature_desc,
        "order_checks": json_load_list(item.order_checks_json),
        "replacement_space": item.replacement_space,
        "barriers": json_load_list(item.barriers_json),
        "highlight_level": item.highlight_level,
        "verification_status": item.verification_status,
        "source_note": item.source_note,
        "data_date": item.data_date,
        "sort_order": item.sort_order,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def get_group(db: Session, group_id: int, include_inactive: bool = False) -> MarketOpportunityGroup:
    group = db.get(MarketOpportunityGroup, group_id)
    if not group or (not include_inactive and not group.is_active):
        raise ValueError("产业方向不存在")
    return group


def clean_required(value: Any, message: str, max_length: int) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if not cleaned:
        raise ValueError(message)
    return cleaned[:max_length]


def clean_optional(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned[:max_length] if cleaned else None


def clean_color(value: Any) -> str:
    cleaned = str(value or DEFAULT_GROUP_COLOR).strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", cleaned):
        return cleaned
    return DEFAULT_GROUP_COLOR


def clean_stock_code(value: Any) -> str | None:
    cleaned = str(value or "").strip().upper()
    return cleaned[:24] if cleaned else None


def clean_status(value: Any) -> str:
    cleaned = clean_required(value, "验证状态不能为空", 24)
    if cleaned not in VERIFICATION_STATUSES:
        raise ValueError("验证状态必须是：待验证、部分验证、已验证、证伪")
    return cleaned


def clean_string_list(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(str(value or "").strip().split())
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned[:300])
    return result


def json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def json_load_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]
