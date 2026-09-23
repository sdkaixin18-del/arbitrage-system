from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AStock,
    IndustryChain,
    IndustryChainCompany,
    IndustryChainEvidence,
    IndustryChainOpportunityLink,
    IndustryChainSegment,
    IndustryChainTask,
    MarketOpportunityGroup,
    MarketOpportunityItem,
    StockResearchGenerationItem,
    WatchlistAnnouncementItem,
    XueqiuPost,
    XueqiuRecommendation,
    now_utc,
)
from app.research_runs import research_skill_meta

DEFAULT_SEGMENT_NAME = "待归类"
CHAIN_PHASES = ["启动期", "扩散期", "确认期", "退潮期", "观察"]
CHAIN_STATUSES = ["active", "paused", "archived"]
COMPANY_STATUSES = ["重点跟踪", "观察", "待验证", "排除"]
TASK_STATUSES = ["待验证", "验证中", "已验证", "证伪"]
PRIORITIES = ["高", "中", "低"]
IMPACT_LEVELS = ["高", "中", "低", "风险"]
AUTO_EVIDENCE_DAYS = 90
AUTO_EVIDENCE_LIMIT = 20


def ensure_industry_chain_seed(db: Session) -> None:
    groups = list(db.scalars(select(MarketOpportunityGroup).where(MarketOpportunityGroup.is_active.is_(True)).order_by(MarketOpportunityGroup.sort_order, MarketOpportunityGroup.id)))
    if not groups:
        return
    changed = False
    for group in groups:
        chain = db.scalar(select(IndustryChain).where(IndustryChain.name == group.name).limit(1))
        if not chain:
            chain = IndustryChain(
                name=group.name,
                summary=group.subtitle,
                phase="观察",
                strength=50,
                status="active",
                sort_order=group.sort_order,
            )
            db.add(chain)
            db.flush()
            changed = True
        segment = default_segment(db, chain, create=True)
        items = list(db.scalars(select(MarketOpportunityItem).where(MarketOpportunityItem.group_id == group.id).order_by(MarketOpportunityItem.sort_order, MarketOpportunityItem.id)))
        for item in items:
            existing_link = db.scalar(select(IndustryChainOpportunityLink).where(IndustryChainOpportunityLink.opportunity_item_id == item.id).limit(1))
            if existing_link:
                continue
            stock = resolve_stock(db, item.stock_code, item.company_name)
            company = find_or_create_company_from_opportunity(db, chain, segment, item, stock)
            db.add(
                IndustryChainOpportunityLink(
                    chain_id=chain.id,
                    segment_id=segment.id if segment else None,
                    company_id=company.id if company else None,
                    opportunity_item_id=item.id,
                )
            )
            changed = True
    if changed:
        db.commit()


def industry_chain_overview(db: Session) -> dict[str, Any]:
    chains = list(active_chains_query(db))
    chain_ids = [chain.id for chain in chains]
    companies_by_chain = count_by_chain(db, IndustryChainCompany, chain_ids)
    segments_by_chain = count_by_chain(db, IndustryChainSegment, chain_ids)
    tasks_by_chain = count_open_tasks_by_chain(db, chain_ids)
    opportunities_by_chain = count_by_chain(db, IndustryChainOpportunityLink, chain_ids)
    latest_evidence_by_chain = latest_manual_evidence_by_chain(db, chain_ids)
    phase_stats: dict[str, int] = {}
    for chain in chains:
        phase_stats[chain.phase] = phase_stats.get(chain.phase, 0) + 1
    updated_at = max([chain.updated_at for chain in chains] + [item.updated_at for item in latest_evidence_by_chain.values()], default=None)
    return {
        "status": "ok" if chains else "empty",
        "updated_at": updated_at,
        "message": f"已维护 {len(chains)} 条产业链、{sum(companies_by_chain.values())} 个公司节点、{sum(tasks_by_chain.values())} 个待验证事项。",
        "phase_stats": phase_stats,
        "task_count": sum(tasks_by_chain.values()),
        "chains": [
            chain_summary_to_out(
                chain,
                segment_count=segments_by_chain.get(chain.id, 0),
                company_count=companies_by_chain.get(chain.id, 0),
                task_count=tasks_by_chain.get(chain.id, 0),
                opportunity_count=opportunities_by_chain.get(chain.id, 0),
                latest_evidence=latest_evidence_by_chain.get(chain.id),
            )
            for chain in chains
        ],
    }


def industry_chain_detail(db: Session, chain_id: int) -> dict[str, Any]:
    chain = get_chain(db, chain_id)
    segments = list(db.scalars(select(IndustryChainSegment).where(IndustryChainSegment.chain_id == chain.id).order_by(IndustryChainSegment.sort_order, IndustryChainSegment.id)))
    companies = list(db.scalars(select(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain.id).order_by(IndustryChainCompany.sort_order, desc(IndustryChainCompany.elasticity_score), IndustryChainCompany.id)))
    evidence = list(db.scalars(select(IndustryChainEvidence).where(IndustryChainEvidence.chain_id == chain.id).order_by(desc(IndustryChainEvidence.evidence_date), desc(IndustryChainEvidence.updated_at), IndustryChainEvidence.id)))
    tasks = list(db.scalars(select(IndustryChainTask).where(IndustryChainTask.chain_id == chain.id).order_by(IndustryChainTask.status, IndustryChainTask.due_date.is_(None), IndustryChainTask.due_date, desc(IndustryChainTask.updated_at))))
    links = list(
        db.execute(
            select(IndustryChainOpportunityLink, MarketOpportunityItem)
            .join(MarketOpportunityItem, MarketOpportunityItem.id == IndustryChainOpportunityLink.opportunity_item_id)
            .where(IndustryChainOpportunityLink.chain_id == chain.id)
            .order_by(MarketOpportunityItem.sort_order, MarketOpportunityItem.id)
        ).all()
    )
    return {
        **chain_to_out(chain),
        "segments": [segment_to_out(segment) for segment in segments],
        "companies": [company_to_out(company) for company in companies],
        "evidence": [manual_evidence_to_out(item) for item in evidence],
        "auto_evidence": auto_evidence_for_chain(db, chain, companies),
        "tasks": [task_to_out(task) for task in tasks],
        "opportunity_links": [opportunity_link_to_out(link, item) for link, item in links],
    }


def create_industry_chain(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    chain = IndustryChain()
    apply_chain_payload(chain, payload, creating=True)
    db.add(chain)
    db.flush()
    db.add(IndustryChainSegment(chain_id=chain.id, name=DEFAULT_SEGMENT_NAME, is_default=True, sort_order=100))
    db.commit()
    db.refresh(chain)
    return industry_chain_detail(db, chain.id)


def update_industry_chain(db: Session, chain_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    chain = get_chain(db, chain_id, include_archived=True)
    apply_chain_payload(chain, payload, creating=False)
    chain.updated_at = now_utc()
    db.commit()
    return industry_chain_detail(db, chain.id)


def delete_industry_chain(db: Session, chain_id: int) -> dict[str, str]:
    chain = get_chain(db, chain_id)
    chain.status = "archived"
    chain.updated_at = now_utc()
    db.commit()
    return {"status": "ok", "message": "产业链已归档"}


def create_segment(db: Session, chain_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    chain = get_chain(db, chain_id)
    segment = IndustryChainSegment(chain_id=chain.id)
    apply_segment_payload(segment, payload, creating=True)
    chain.updated_at = now_utc()
    db.add(segment)
    db.commit()
    db.refresh(segment)
    return segment_to_out(segment)


def update_segment(db: Session, segment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    segment = get_segment(db, segment_id)
    apply_segment_payload(segment, payload, creating=False)
    segment.updated_at = now_utc()
    segment.chain.updated_at = now_utc()
    db.commit()
    db.refresh(segment)
    return segment_to_out(segment)


def delete_segment(db: Session, segment_id: int) -> dict[str, str]:
    segment = get_segment(db, segment_id)
    if segment.is_default:
        raise ValueError("默认环节不能删除")
    segment.chain.updated_at = now_utc()
    db.delete(segment)
    db.commit()
    return {"status": "ok", "message": "环节已删除"}


def create_company(db: Session, chain_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    chain = get_chain(db, chain_id)
    company = IndustryChainCompany(chain_id=chain.id)
    apply_company_payload(db, company, payload, creating=True)
    chain.updated_at = now_utc()
    db.add(company)
    db.commit()
    db.refresh(company)
    return company_to_out(company)


def update_company(db: Session, company_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    company = get_company(db, company_id)
    apply_company_payload(db, company, payload, creating=False)
    company.updated_at = now_utc()
    company.chain.updated_at = now_utc()
    db.commit()
    db.refresh(company)
    return company_to_out(company)


def delete_company(db: Session, company_id: int) -> dict[str, str]:
    company = get_company(db, company_id)
    company.chain.updated_at = now_utc()
    db.delete(company)
    db.commit()
    return {"status": "ok", "message": "公司节点已删除"}


def create_evidence(db: Session, chain_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    chain = get_chain(db, chain_id)
    evidence = IndustryChainEvidence(chain_id=chain.id)
    apply_evidence_payload(db, evidence, payload, creating=True)
    chain.updated_at = now_utc()
    db.add(evidence)
    db.commit()
    db.refresh(evidence)
    return manual_evidence_to_out(evidence)


def update_evidence(db: Session, evidence_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    evidence = get_evidence(db, evidence_id)
    apply_evidence_payload(db, evidence, payload, creating=False)
    evidence.updated_at = now_utc()
    evidence.chain.updated_at = now_utc()
    db.commit()
    db.refresh(evidence)
    return manual_evidence_to_out(evidence)


def delete_evidence(db: Session, evidence_id: int) -> dict[str, str]:
    evidence = get_evidence(db, evidence_id)
    evidence.chain.updated_at = now_utc()
    db.delete(evidence)
    db.commit()
    return {"status": "ok", "message": "证据已删除"}


def create_task(db: Session, chain_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    chain = get_chain(db, chain_id)
    task = IndustryChainTask(chain_id=chain.id)
    apply_task_payload(db, task, payload, creating=True)
    chain.updated_at = now_utc()
    db.add(task)
    db.commit()
    db.refresh(task)
    return task_to_out(task)


def update_task(db: Session, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    task = get_task(db, task_id)
    apply_task_payload(db, task, payload, creating=False)
    task.updated_at = now_utc()
    task.chain.updated_at = now_utc()
    db.commit()
    db.refresh(task)
    return task_to_out(task)


def delete_task(db: Session, task_id: int) -> dict[str, str]:
    task = get_task(db, task_id)
    task.chain.updated_at = now_utc()
    db.delete(task)
    db.commit()
    return {"status": "ok", "message": "待验证事项已删除"}


def create_opportunity_link(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    chain = get_chain(db, int(payload.get("chain_id") or 0))
    opportunity = db.get(MarketOpportunityItem, int(payload.get("opportunity_item_id") or 0))
    if not opportunity:
        raise ValueError("机会矩阵条目不存在")
    existing = db.scalar(select(IndustryChainOpportunityLink).where(IndustryChainOpportunityLink.opportunity_item_id == opportunity.id).limit(1))
    if existing:
        raise ValueError("该机会矩阵条目已经关联产业链")
    segment_id = payload.get("segment_id")
    company_id = payload.get("company_id")
    if segment_id:
        get_segment(db, int(segment_id), chain_id=chain.id)
    if company_id:
        get_company(db, int(company_id), chain_id=chain.id)
    link = IndustryChainOpportunityLink(
        chain_id=chain.id,
        segment_id=int(segment_id) if segment_id else None,
        company_id=int(company_id) if company_id else None,
        opportunity_item_id=opportunity.id,
    )
    db.add(link)
    chain.updated_at = now_utc()
    db.commit()
    return {"status": "ok", "message": "机会矩阵已关联"}


def delete_opportunity_link(db: Session, link_id: int) -> dict[str, str]:
    link = db.get(IndustryChainOpportunityLink, link_id)
    if not link:
        raise ValueError("关联不存在")
    chain = db.get(IndustryChain, link.chain_id)
    if chain:
        chain.updated_at = now_utc()
    db.delete(link)
    db.commit()
    return {"status": "ok", "message": "机会矩阵关联已删除"}


def active_chains_query(db: Session):
    return db.scalars(select(IndustryChain).where(IndustryChain.status != "archived").order_by(IndustryChain.sort_order, desc(IndustryChain.strength), IndustryChain.id))


def count_by_chain(db: Session, model: Any, chain_ids: list[int]) -> dict[int, int]:
    if not chain_ids:
        return {}
    rows = db.execute(select(model.chain_id, func.count(model.id)).where(model.chain_id.in_(chain_ids)).group_by(model.chain_id)).all()
    return {int(chain_id): int(count) for chain_id, count in rows}


def count_open_tasks_by_chain(db: Session, chain_ids: list[int]) -> dict[int, int]:
    if not chain_ids:
        return {}
    rows = db.execute(
        select(IndustryChainTask.chain_id, func.count(IndustryChainTask.id))
        .where(IndustryChainTask.chain_id.in_(chain_ids), IndustryChainTask.status.in_(["待验证", "验证中"]))
        .group_by(IndustryChainTask.chain_id)
    ).all()
    return {int(chain_id): int(count) for chain_id, count in rows}


def latest_manual_evidence_by_chain(db: Session, chain_ids: list[int]) -> dict[int, IndustryChainEvidence]:
    if not chain_ids:
        return {}
    rows = list(
        db.scalars(
            select(IndustryChainEvidence)
            .where(IndustryChainEvidence.chain_id.in_(chain_ids))
            .order_by(IndustryChainEvidence.chain_id, desc(IndustryChainEvidence.evidence_date), desc(IndustryChainEvidence.updated_at))
        )
    )
    result: dict[int, IndustryChainEvidence] = {}
    for row in rows:
        result.setdefault(row.chain_id, row)
    return result


def default_segment(db: Session, chain: IndustryChain, create: bool = False) -> IndustryChainSegment | None:
    segment = db.scalar(select(IndustryChainSegment).where(IndustryChainSegment.chain_id == chain.id, IndustryChainSegment.is_default.is_(True)).limit(1))
    if not segment:
        segment = db.scalar(select(IndustryChainSegment).where(IndustryChainSegment.chain_id == chain.id).order_by(IndustryChainSegment.sort_order, IndustryChainSegment.id).limit(1))
    if not segment and create:
        segment = IndustryChainSegment(chain_id=chain.id, name=DEFAULT_SEGMENT_NAME, is_default=True, sort_order=100)
        db.add(segment)
        db.flush()
    return segment


def resolve_stock(db: Session, stock_code: str | None, company_name: str | None = None) -> AStock | None:
    code = clean_text(stock_code, 24)
    if code:
        normalized = code.upper()
        stock = db.scalar(select(AStock).where(or_(AStock.full_code == normalized, AStock.code == normalized[-6:])).limit(1))
        if stock:
            return stock
    name = clean_text(company_name, 120)
    if name:
        return db.scalar(select(AStock).where(AStock.name == name).limit(1))
    return None


def find_or_create_company_from_opportunity(
    db: Session,
    chain: IndustryChain,
    segment: IndustryChainSegment | None,
    item: MarketOpportunityItem,
    stock: AStock | None,
) -> IndustryChainCompany | None:
    full_code = stock.full_code if stock else None
    if full_code:
        company = db.scalar(select(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain.id, IndustryChainCompany.full_code == full_code).limit(1))
    else:
        company = db.scalar(select(IndustryChainCompany).where(IndustryChainCompany.chain_id == chain.id, IndustryChainCompany.name == item.company_name).limit(1))
    if company:
        return company
    company = IndustryChainCompany(
        chain_id=chain.id,
        segment_id=segment.id if segment else None,
        code=stock.code if stock else clean_text(item.stock_code, 16),
        name=stock.name if stock else clean_required(item.company_name, "公司名称不能为空", 120),
        exchange=stock.exchange if stock else None,
        full_code=stock.full_code if stock else None,
        position=item.feature_title,
        elasticity_score=70 if item.highlight_level >= 2 else 60 if item.highlight_level == 1 else 50,
        tracking_status="待验证" if item.verification_status == "待验证" else "观察",
        core_logic=item.feature_desc,
        main_risk="；".join(json_like_list(item.barriers_json)) if item.barriers_json else None,
        sort_order=item.sort_order,
    )
    db.add(company)
    db.flush()
    return company


def auto_evidence_for_chain(db: Session, chain: IndustryChain, companies: list[IndustryChainCompany]) -> list[dict[str, Any]]:
    company_names = [company.name for company in companies if company.name]
    full_codes = [company.full_code for company in companies if company.full_code]
    codes = [company.code for company in companies if company.code]
    if not company_names and not full_codes and not codes:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=AUTO_EVIDENCE_DAYS)
    result: list[dict[str, Any]] = []
    if full_codes:
        research_rows = list(
            db.scalars(
                select(StockResearchGenerationItem)
                .where(StockResearchGenerationItem.full_code.in_(full_codes), StockResearchGenerationItem.created_at >= since)
                .order_by(desc(StockResearchGenerationItem.created_at))
                .limit(AUTO_EVIDENCE_LIMIT)
            )
        )
        for row in research_rows:
            meta = research_skill_meta(row.run.template_name if row.run else "qq")
            result.append(auto_evidence_item(f"research-{row.id}", meta["source_type"], f"{meta['label']} Skill", row.summary or f"{row.name} 生成结论", row.body, f"/research-runs/{row.run_id}", row.created_at, row.name, row.full_code))

        ann_rows = list(
            db.scalars(
                select(WatchlistAnnouncementItem)
                .where(WatchlistAnnouncementItem.full_code.in_(full_codes), (WatchlistAnnouncementItem.published_at >= since) | (WatchlistAnnouncementItem.crawled_at >= since))
                .order_by(desc(WatchlistAnnouncementItem.published_at), desc(WatchlistAnnouncementItem.crawled_at))
                .limit(AUTO_EVIDENCE_LIMIT)
            )
        )
        for row in ann_rows:
            result.append(auto_evidence_item(f"announcement-{row.id}", row.source_type, row.source_name, row.title, row.summary, row.source_url, row.published_at or row.crawled_at, row.name, row.full_code))

        rec_rows = list(
            db.scalars(
                select(XueqiuRecommendation)
                .where(XueqiuRecommendation.full_code.in_(full_codes), XueqiuRecommendation.created_at >= since)
                .order_by(desc(XueqiuRecommendation.created_at))
                .limit(AUTO_EVIDENCE_LIMIT)
            )
        )
        for row in rec_rows:
            result.append(auto_evidence_item(f"xueqiu-rec-{row.id}", "xueqiu_recommendation", "雪球推荐", row.stock_name, row.source_excerpt, row.source_url, row.created_at, row.stock_name, row.full_code))

    post_conditions = []
    for name in company_names[:20]:
        post_conditions.append(XueqiuPost.content.contains(name))
    for code in codes[:20]:
        post_conditions.append(XueqiuPost.content.contains(code))
    if post_conditions:
        post_rows = list(
            db.scalars(
                select(XueqiuPost)
                .where(or_(*post_conditions), or_(XueqiuPost.published_at >= since, XueqiuPost.crawled_at >= since))
                .order_by(desc(XueqiuPost.published_at), desc(XueqiuPost.crawled_at))
                .limit(AUTO_EVIDENCE_LIMIT)
            )
        )
        for row in post_rows:
            result.append(auto_evidence_item(f"xueqiu-post-{row.id}", "xueqiu_post", row.author_name, "雪球发言", row.content, row.source_url, row.published_at or row.crawled_at, matched_company_name(row.content, companies), None))

    link_rows = list(
        db.execute(
            select(IndustryChainOpportunityLink, MarketOpportunityItem)
            .join(MarketOpportunityItem, MarketOpportunityItem.id == IndustryChainOpportunityLink.opportunity_item_id)
            .where(IndustryChainOpportunityLink.chain_id == chain.id)
            .limit(AUTO_EVIDENCE_LIMIT)
        ).all()
    )
    for link, item in link_rows:
        result.append(auto_evidence_item(f"opportunity-{link.id}", "opportunity_map", "产业机会矩阵", item.feature_title or item.company_name, item.feature_desc or item.source_note, "/industry-chain/opportunity-map", item.updated_at, item.company_name, item.stock_code))
    result.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return result[: AUTO_EVIDENCE_LIMIT * 4]


def matched_company_name(content: str, companies: list[IndustryChainCompany]) -> str | None:
    for company in companies:
        if company.name and company.name in content:
            return company.name
    return None


def auto_evidence_item(
    item_id: str,
    source_type: str,
    source_name: str,
    title: str,
    content: str | None,
    link: str | None,
    created_at: datetime | None,
    company_name: str | None,
    full_code: str | None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "source_type": source_type,
        "source_name": source_name,
        "title": title,
        "content": content,
        "link": link,
        "created_at": created_at,
        "company_name": company_name,
        "full_code": full_code,
    }


def apply_chain_payload(chain: IndustryChain, payload: dict[str, Any], creating: bool) -> None:
    if creating or "name" in payload:
        chain.name = clean_required(payload.get("name"), "产业链名称不能为空", 100)
    if creating or "summary" in payload:
        chain.summary = clean_optional(payload.get("summary"), 4000)
    if creating or "phase" in payload:
        chain.phase = clean_choice(payload.get("phase") or "观察", CHAIN_PHASES, "阶段")
    if creating or "strength" in payload:
        chain.strength = clamp_int(payload.get("strength"), 0, 100, 50)
    if creating or "catalyst" in payload:
        chain.catalyst = clean_optional(payload.get("catalyst"), 4000)
    if creating or "risk" in payload:
        chain.risk = clean_optional(payload.get("risk"), 4000)
    if creating or "status" in payload:
        chain.status = clean_choice(payload.get("status") or "active", CHAIN_STATUSES, "状态")
    if creating or "sort_order" in payload:
        chain.sort_order = int(payload.get("sort_order") or 100)


def apply_segment_payload(segment: IndustryChainSegment, payload: dict[str, Any], creating: bool) -> None:
    if creating or "name" in payload:
        segment.name = clean_required(payload.get("name"), "环节名称不能为空", 80)
    if creating or "description" in payload:
        segment.description = clean_optional(payload.get("description"), 4000)
    if creating or "sort_order" in payload:
        segment.sort_order = int(payload.get("sort_order") or 100)


def apply_company_payload(db: Session, company: IndustryChainCompany, payload: dict[str, Any], creating: bool) -> None:
    segment_id = payload.get("segment_id")
    if creating or "segment_id" in payload:
        if segment_id:
            get_segment(db, int(segment_id), chain_id=company.chain_id)
            company.segment_id = int(segment_id)
        else:
            company.segment_id = None
    stock = resolve_stock(db, payload.get("stock_code") or payload.get("full_code"), payload.get("name"))
    if stock:
        company.code = stock.code
        company.name = stock.name
        company.exchange = stock.exchange
        company.full_code = stock.full_code
    else:
        if creating or "name" in payload:
            company.name = clean_required(payload.get("name"), "公司名称不能为空", 120)
        if creating or "stock_code" in payload or "full_code" in payload:
            code = clean_text(payload.get("stock_code") or payload.get("full_code"), 16)
            company.code = code[-6:] if code else None
            company.full_code = code if code and len(code) > 6 else None
            company.exchange = code[:2] if code and len(code) > 6 else None
    if creating or "position" in payload:
        company.position = clean_optional(payload.get("position"), 4000)
    if creating or "elasticity_score" in payload:
        company.elasticity_score = clamp_int(payload.get("elasticity_score"), 0, 100, 50)
    if creating or "tracking_status" in payload:
        company.tracking_status = clean_choice(payload.get("tracking_status") or "观察", COMPANY_STATUSES, "跟踪状态")
    if creating or "core_logic" in payload:
        company.core_logic = clean_optional(payload.get("core_logic"), 4000)
    if creating or "main_risk" in payload:
        company.main_risk = clean_optional(payload.get("main_risk"), 4000)
    if creating or "sort_order" in payload:
        company.sort_order = int(payload.get("sort_order") or 100)


def apply_evidence_payload(db: Session, evidence: IndustryChainEvidence, payload: dict[str, Any], creating: bool) -> None:
    company_id = payload.get("company_id")
    if creating or "company_id" in payload:
        if company_id:
            get_company(db, int(company_id), chain_id=evidence.chain_id)
            evidence.company_id = int(company_id)
        else:
            evidence.company_id = None
    if creating or "title" in payload:
        evidence.title = clean_required(payload.get("title"), "证据标题不能为空", 240)
    if creating or "content" in payload:
        evidence.content = clean_optional(payload.get("content"), 4000)
    if creating or "source_name" in payload:
        evidence.source_name = clean_optional(payload.get("source_name"), 120)
    if creating or "source_url" in payload:
        evidence.source_url = clean_optional(payload.get("source_url"), 1000)
    if creating or "impact_level" in payload:
        evidence.impact_level = clean_choice(payload.get("impact_level") or "中", IMPACT_LEVELS, "影响等级")
    if creating or "evidence_date" in payload:
        evidence.evidence_date = parse_date(payload.get("evidence_date")) or date.today()


def apply_task_payload(db: Session, task: IndustryChainTask, payload: dict[str, Any], creating: bool) -> None:
    company_id = payload.get("company_id")
    if creating or "company_id" in payload:
        if company_id:
            get_company(db, int(company_id), chain_id=task.chain_id)
            task.company_id = int(company_id)
        else:
            task.company_id = None
    if creating or "title" in payload:
        task.title = clean_required(payload.get("title"), "待验证标题不能为空", 240)
    if creating or "description" in payload:
        task.description = clean_optional(payload.get("description"), 4000)
    if creating or "priority" in payload:
        task.priority = clean_choice(payload.get("priority") or "中", PRIORITIES, "优先级")
    if creating or "status" in payload:
        task.status = clean_choice(payload.get("status") or "待验证", TASK_STATUSES, "状态")
    if creating or "due_date" in payload:
        task.due_date = parse_date(payload.get("due_date"))
    if creating or "conclusion" in payload:
        task.conclusion = clean_optional(payload.get("conclusion"), 4000)


def chain_summary_to_out(
    chain: IndustryChain,
    segment_count: int,
    company_count: int,
    task_count: int,
    opportunity_count: int,
    latest_evidence: IndustryChainEvidence | None,
) -> dict[str, Any]:
    return {
        **chain_to_out(chain),
        "segment_count": segment_count,
        "company_count": company_count,
        "open_task_count": task_count,
        "opportunity_count": opportunity_count,
        "latest_evidence": manual_evidence_to_out(latest_evidence) if latest_evidence else None,
    }


def chain_to_out(chain: IndustryChain) -> dict[str, Any]:
    return {
        "id": chain.id,
        "name": chain.name,
        "summary": chain.summary,
        "phase": chain.phase,
        "strength": chain.strength,
        "catalyst": chain.catalyst,
        "risk": chain.risk,
        "status": chain.status,
        "sort_order": chain.sort_order,
        "created_at": chain.created_at,
        "updated_at": chain.updated_at,
    }


def segment_to_out(segment: IndustryChainSegment) -> dict[str, Any]:
    return {
        "id": segment.id,
        "chain_id": segment.chain_id,
        "name": segment.name,
        "description": segment.description,
        "sort_order": segment.sort_order,
        "is_default": segment.is_default,
        "created_at": segment.created_at,
        "updated_at": segment.updated_at,
    }


def company_to_out(company: IndustryChainCompany) -> dict[str, Any]:
    return {
        "id": company.id,
        "chain_id": company.chain_id,
        "segment_id": company.segment_id,
        "code": company.code,
        "name": company.name,
        "exchange": company.exchange,
        "full_code": company.full_code,
        "position": company.position,
        "elasticity_score": company.elasticity_score,
        "tracking_status": company.tracking_status,
        "core_logic": company.core_logic,
        "main_risk": company.main_risk,
        "sort_order": company.sort_order,
        "created_at": company.created_at,
        "updated_at": company.updated_at,
    }


def manual_evidence_to_out(evidence: IndustryChainEvidence | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    return {
        "id": evidence.id,
        "chain_id": evidence.chain_id,
        "company_id": evidence.company_id,
        "title": evidence.title,
        "content": evidence.content,
        "source_name": evidence.source_name,
        "source_url": evidence.source_url,
        "impact_level": evidence.impact_level,
        "evidence_date": evidence.evidence_date,
        "created_at": evidence.created_at,
        "updated_at": evidence.updated_at,
    }


def task_to_out(task: IndustryChainTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "chain_id": task.chain_id,
        "company_id": task.company_id,
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "status": task.status,
        "due_date": task.due_date,
        "conclusion": task.conclusion,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def opportunity_link_to_out(link: IndustryChainOpportunityLink, item: MarketOpportunityItem) -> dict[str, Any]:
    return {
        "id": link.id,
        "chain_id": link.chain_id,
        "segment_id": link.segment_id,
        "company_id": link.company_id,
        "opportunity_item_id": link.opportunity_item_id,
        "company_name": item.company_name,
        "stock_code": item.stock_code,
        "feature_title": item.feature_title,
        "feature_desc": item.feature_desc,
        "verification_status": item.verification_status,
        "source_note": item.source_note,
        "updated_at": item.updated_at,
    }


def get_chain(db: Session, chain_id: int, include_archived: bool = False) -> IndustryChain:
    chain = db.get(IndustryChain, chain_id)
    if not chain or (chain.status == "archived" and not include_archived):
        raise ValueError("产业链不存在")
    return chain


def get_segment(db: Session, segment_id: int, chain_id: int | None = None) -> IndustryChainSegment:
    segment = db.get(IndustryChainSegment, segment_id)
    if not segment or (chain_id is not None and segment.chain_id != chain_id):
        raise ValueError("产业链环节不存在")
    return segment


def get_company(db: Session, company_id: int, chain_id: int | None = None) -> IndustryChainCompany:
    company = db.get(IndustryChainCompany, company_id)
    if not company or (chain_id is not None and company.chain_id != chain_id):
        raise ValueError("公司节点不存在")
    return company


def get_evidence(db: Session, evidence_id: int) -> IndustryChainEvidence:
    evidence = db.get(IndustryChainEvidence, evidence_id)
    if not evidence:
        raise ValueError("证据不存在")
    return evidence


def get_task(db: Session, task_id: int) -> IndustryChainTask:
    task = db.get(IndustryChainTask, task_id)
    if not task:
        raise ValueError("待验证事项不存在")
    return task


def clean_required(value: Any, message: str, max_length: int) -> str:
    cleaned = clean_text(value, max_length)
    if not cleaned:
        raise ValueError(message)
    return cleaned


def clean_optional(value: Any, max_length: int) -> str | None:
    return clean_text(value, max_length) or None


def clean_text(value: Any, max_length: int) -> str:
    return " ".join(str(value or "").strip().split())[:max_length]


def clean_choice(value: Any, allowed: list[str], label: str) -> str:
    cleaned = clean_required(value, f"{label}不能为空", 40)
    if cleaned not in allowed:
        raise ValueError(f"{label}必须是：{'、'.join(allowed)}")
    return cleaned


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def json_like_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    import json

    try:
        value = json.loads(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
