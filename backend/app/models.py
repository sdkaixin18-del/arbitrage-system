from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class XueqiuTarget(Base):
    __tablename__ = "xq_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    nickname: Mapped[str] = mapped_column(String(120), nullable=False)
    xueqiu_user_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    watchlist_url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)
    last_home_feed_status: Mapped[str] = mapped_column(String(40), default="not_configured", nullable=False)
    last_home_feed_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_home_feed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_watchlist_status: Mapped[str] = mapped_column(String(40), default="not_configured", nullable=False)
    last_watchlist_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_watchlist_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    posts: Mapped[list["XueqiuPost"]] = relationship(back_populates="target", cascade="all, delete-orphan")
    snapshots: Mapped[list["XueqiuWatchlistSnapshot"]] = relationship(back_populates="target", cascade="all, delete-orphan")
    events: Mapped[list["XueqiuWatchlistEvent"]] = relationship(back_populates="target", cascade="all, delete-orphan")
    recommendations: Mapped[list["XueqiuRecommendation"]] = relationship(back_populates="target", cascade="all, delete-orphan")
    push_logs: Mapped[list["XueqiuPushLog"]] = relationship(back_populates="target", cascade="all, delete-orphan")
    crawl_logs: Mapped[list["XueqiuCrawlLog"]] = relationship(back_populates="target", cascade="all, delete-orphan")


class XueqiuPost(Base):
    __tablename__ = "xq_posts"
    __table_args__ = (UniqueConstraint("target_id", "external_id", name="uq_xq_post_target_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("xq_targets.id", ondelete="CASCADE"), index=True, nullable=False)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    author_user_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    target: Mapped[XueqiuTarget] = relationship(back_populates="posts")


class XueqiuWatchlistSnapshot(Base):
    __tablename__ = "xq_watchlist_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("xq_targets.id", ondelete="CASCADE"), index=True, nullable=False)
    stock_code: Mapped[str] = mapped_column(String(16), nullable=False)
    stock_name: Mapped[str] = mapped_column(String(80), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    target: Mapped[XueqiuTarget] = relationship(back_populates="snapshots")


class XueqiuWatchlistEvent(Base):
    __tablename__ = "xq_watchlist_events"
    __table_args__ = (UniqueConstraint("target_id", "event_type", "stock_code", "created_at", name="uq_xq_watchlist_event_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("xq_targets.id", ondelete="CASCADE"), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    stock_code: Mapped[str] = mapped_column(String(16), nullable=False)
    stock_name: Mapped[str] = mapped_column(String(80), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    push_status: Mapped[str] = mapped_column(String(40), default="not_configured", nullable=False)
    push_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    target: Mapped[XueqiuTarget] = relationship(back_populates="events")


class XueqiuRecommendation(Base):
    __tablename__ = "xq_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("xq_targets.id", ondelete="CASCADE"), index=True, nullable=False)
    source_post_id: Mapped[int | None] = mapped_column(ForeignKey("xq_posts.id", ondelete="SET NULL"), index=True, nullable=True)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False, default="auto")
    stock_code: Mapped[str] = mapped_column(String(16), nullable=False)
    stock_name: Mapped[str] = mapped_column(String(80), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    start_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open", index=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    target: Mapped[XueqiuTarget] = relationship(back_populates="recommendations")
    source_post: Mapped[XueqiuPost | None] = relationship()


class XueqiuPushLog(Base):
    __tablename__ = "xq_push_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("xq_targets.id", ondelete="CASCADE"), index=True, nullable=True)
    group_name: Mapped[str] = mapped_column(String(40), nullable=False, default="雪球自选")
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    target: Mapped[XueqiuTarget | None] = relationship(back_populates="push_logs")


class XueqiuCrawlLog(Base):
    __tablename__ = "xq_crawl_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("xq_targets.id", ondelete="CASCADE"), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ignored_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    target: Mapped[XueqiuTarget | None] = relationship(back_populates="crawl_logs")


class AStock(Base):
    __tablename__ = "a_stocks"
    __table_args__ = (
        UniqueConstraint("code", name="uq_a_stock_code"),
        UniqueConstraint("full_code", name="uq_a_stock_full_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    pinyin: Mapped[str] = mapped_column(String(120), nullable=False, default="", index=True)
    org_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="akshare")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FactorTag(Base):
    __tablename__ = "factor_tags"
    __table_args__ = (UniqueConstraint("name", name="uq_factor_tag_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    color: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    stock_links: Mapped[list["FactorStockTag"]] = relationship(back_populates="tag", cascade="all, delete-orphan")


class FactorStockTag(Base):
    __tablename__ = "factor_stock_tags"
    __table_args__ = (UniqueConstraint("full_code", "tag_id", name="uq_factor_stock_tag_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("factor_tags.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    tag: Mapped[FactorTag] = relationship(back_populates="stock_links")


class FactorExcludedStock(Base):
    __tablename__ = "factor_excluded_stocks"
    __table_args__ = (UniqueConstraint("full_code", name="uq_factor_excluded_stock_full_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FactorStockReview(Base):
    __tablename__ = "factor_stock_reviews"
    __table_args__ = (UniqueConstraint("full_code", name="uq_factor_stock_review_full_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unreviewed", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_reasons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_reason_signature: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reviewed_tag_signature: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FactorReviewBatch(Base):
    __tablename__ = "factor_review_batches"
    __table_args__ = (
        UniqueConstraint("trade_date", "gain_limit", "amount_limit", name="uq_factor_review_batch_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    gain_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    amount_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok")
    source_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    stock_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    items: Mapped[list["FactorReviewItem"]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class FactorReviewItem(Base):
    __tablename__ = "factor_review_items"
    __table_args__ = (UniqueConstraint("batch_id", "full_code", name="uq_factor_review_item_stock"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("factor_review_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    gain_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    amount_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    market_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    suggested_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_status_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    research_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    researched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    initial_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    draft_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confirmed_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="initial_pending", index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    initial_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False, index=True)

    batch: Mapped[FactorReviewBatch] = relationship(back_populates="items")
    versions: Mapped[list["FactorReviewVersion"]] = relationship(back_populates="item", cascade="all, delete-orphan")


class FactorReviewVersion(Base):
    __tablename__ = "factor_review_versions"
    __table_args__ = (UniqueConstraint("item_id", "revision", name="uq_factor_review_version_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("factor_review_items.id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)

    item: Mapped[FactorReviewItem] = relationship(back_populates="versions")


class FactorPeriodPreset(Base):
    __tablename__ = "factor_period_presets"
    __table_args__ = (UniqueConstraint("name", name="uq_factor_period_preset_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    periods_json: Mapped[str] = mapped_column(Text, nullable=False, default="[1,2,3,5]")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FactorQuoteSnapshot(Base):
    __tablename__ = "factor_quote_snapshots"
    __table_args__ = (UniqueConstraint("full_code", name="uq_factor_quote_full_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    latest_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    returns_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False, index=True)


class StockResearchWorkspace(Base):
    __tablename__ = "stock_research_workspaces"
    __table_args__ = (UniqueConstraint("full_code", name="uq_stock_research_workspace_full_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    research_status: Mapped[str] = mapped_column(String(32), nullable=False, default="待研究", index=True)
    current_conclusion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    chart_expression: Mapped[str] = mapped_column(Text, nullable=False, default="")
    fundamental_change: Mapped[str] = mapped_column(Text, nullable=False, default="")
    market_trading: Mapped[str] = mapped_column(Text, nullable=False, default="")
    industry_position: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risks_and_invalidation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    next_validation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False, index=True)

    evidence: Mapped[list["StockResearchEvidence"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    versions: Mapped[list["StockResearchVersion"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class StockResearchEvidence(Base):
    __tablename__ = "stock_research_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("stock_research_workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confirmation_status: Mapped[str] = mapped_column(String(24), nullable=False, default="待确认", index=True)
    evidence_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    workspace: Mapped[StockResearchWorkspace] = relationship(back_populates="evidence")


class StockResearchVersion(Base):
    __tablename__ = "stock_research_versions"
    __table_args__ = (UniqueConstraint("workspace_id", "revision", name="uq_stock_research_version_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("stock_research_workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)

    workspace: Mapped[StockResearchWorkspace] = relationship(back_populates="versions")


class StockResearchGenerationRun(Base):
    __tablename__ = "stock_research_generation_runs"
    __table_args__ = (UniqueConstraint("source_ref", name="uq_stock_research_run_source_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    template_name: Mapped[str] = mapped_column(String(80), nullable=False, default="qq")
    template_version: Mapped[str] = mapped_column(String(40), nullable=False, default="qq-v1")
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ignored_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    items: Mapped[list["StockResearchGenerationItem"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class StockResearchGenerationItem(Base):
    __tablename__ = "stock_research_generation_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("stock_research_generation_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="generated", index=True)
    action_status: Mapped[str] = mapped_column(String(40), nullable=False, default="unprocessed", index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    market_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    suggested_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    selected_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    suggested_market_style: Mapped[str | None] = mapped_column(String(120), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="待验证")
    source_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_record_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    tags_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ignored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    run: Mapped[StockResearchGenerationRun] = relationship(back_populates="items")


class StockAnalysisRecord(Base):
    __tablename__ = "stock_analysis_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_note", index=True)
    source_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="待验证")
    suggested_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FactorRefreshRun(Base):
    __tablename__ = "factor_refresh_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FactorTagCandidateRun(Base):
    __tablename__ = "factor_tag_candidate_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_only", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FactorAutoTagRun(Base):
    __tablename__ = "factor_auto_tag_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_only", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    stock_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tagged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tag_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_status_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FactorImaTagCache(Base):
    __tablename__ = "factor_ima_tag_cache"
    __table_args__ = (UniqueConstraint("full_code", name="uq_factor_ima_tag_cache_full_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FactorMarketCapSnapshot(Base):
    __tablename__ = "factor_market_cap_snapshots"
    __table_args__ = (UniqueConstraint("full_code", name="uq_factor_market_cap_full_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    total_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    float_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class SectorIndex(Base):
    __tablename__ = "sector_indices"
    __table_args__ = (UniqueConstraint("name", name="uq_sector_index_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_name: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    group_sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    source_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    base_value: Mapped[float] = mapped_column(Float, nullable=False, default=1000.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    members: Mapped[list["SectorIndexMember"]] = relationship(back_populates="sector", cascade="all, delete-orphan")
    bars: Mapped[list["SectorIndexBar"]] = relationship(back_populates="sector", cascade="all, delete-orphan")


class SectorIndexMember(Base):
    __tablename__ = "sector_index_members"
    __table_args__ = (UniqueConstraint("sector_id", "full_code", name="uq_sector_index_member_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sector_id: Mapped[int] = mapped_column(ForeignKey("sector_indices.id", ondelete="CASCADE"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", index=True)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    sector: Mapped[SectorIndex] = relationship(back_populates="members")


class SectorIndexBar(Base):
    __tablename__ = "sector_index_bars"
    __table_args__ = (UniqueConstraint("sector_id", "trade_date", name="uq_sector_index_bar_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sector_id: Mapped[int] = mapped_column(ForeignKey("sector_indices.id", ondelete="CASCADE"), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    sector: Mapped[SectorIndex] = relationship(back_populates="bars")


class StockDailyBar(Base):
    __tablename__ = "stock_daily_bars"
    __table_args__ = (UniqueConstraint("full_code", "trade_date", name="uq_stock_daily_bar_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Runtime queries use (full_code, trade_date) or trade_date. The unique
    # composite index already covers full_code as its left-most prefix; keeping
    # additional code/name/exchange/full_code indexes added hundreds of MB of
    # write amplification without serving an application query.
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class MarketStyleEffectSnapshot(Base):
    __tablename__ = "market_style_effect_snapshots"
    __table_args__ = (UniqueConstraint("scope_key", "trade_date", name="uq_market_style_effect_snapshot_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effect_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    breadth_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    sentiment_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    leadership_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    persistence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    market_state: Mapped[str] = mapped_column(String(40), nullable=False, default="等待数据", index=True)
    components_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_flags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FactorEffectDailyLog(Base):
    __tablename__ = "factor_effect_daily_logs"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_factor_effect_daily_log_trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok", index=True)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok", index=True)
    phase_key: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    phase_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    filtered_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    performance_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class MarketStyleCacheRefreshRun(Base):
    __tablename__ = "market_style_cache_refresh_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="running", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketStyleDailyBar(Base):
    __tablename__ = "market_style_daily_bars"
    __table_args__ = (UniqueConstraint("full_code", "trade_date", name="uq_market_style_daily_bar_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)


class MarketStyleThsGroup(Base):
    __tablename__ = "market_style_ths_groups"
    __table_args__ = (UniqueConstraint("group_type", "code", name="uq_market_style_ths_group_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)


class MarketStyleThsBar(Base):
    __tablename__ = "market_style_ths_bars"
    __table_args__ = (UniqueConstraint("group_type", "group_code", "trade_date", name="uq_market_style_ths_bar_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    group_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    group_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)


class MarketStyleThsMember(Base):
    __tablename__ = "market_style_ths_members"
    __table_args__ = (UniqueConstraint("group_type", "group_name", "full_code", name="uq_market_style_ths_member_once"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    group_code: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    group_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)


class MarketOpportunityGroup(Base):
    __tablename__ = "market_opportunity_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    subtitle: Mapped[str | None] = mapped_column(String(160), nullable=True)
    color: Mapped[str | None] = mapped_column(String(24), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    items: Mapped[list["MarketOpportunityItem"]] = relationship(back_populates="group", cascade="all, delete-orphan")


class MarketOpportunityItem(Base):
    __tablename__ = "market_opportunity_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("market_opportunity_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    stock_code: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    feature_title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    feature_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    feature_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_checks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    replacement_space: Mapped[str | None] = mapped_column(Text, nullable=True)
    barriers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    highlight_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    source_note: Mapped[str] = mapped_column(Text, nullable=False)
    data_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    group: Mapped[MarketOpportunityGroup] = relationship(back_populates="items")


class IndustryChain(Base):
    __tablename__ = "industry_chains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str] = mapped_column(String(40), nullable=False, default="观察", index=True)
    strength: Mapped[int] = mapped_column(Integer, nullable=False, default=50, index=True)
    catalyst: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk: Mapped[str | None] = mapped_column(Text, nullable=True)
    investment_logic: Mapped[str | None] = mapped_column(Text, nullable=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_now: Mapped[str | None] = mapped_column(Text, nullable=True)
    drivers_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    expected_duration: Mapped[str | None] = mapped_column(String(120), nullable=True)
    attention_level: Mapped[str] = mapped_column(String(32), nullable=False, default="观察", index=True)
    direction_verdict: Mapped[str] = mapped_column(String(16), nullable=False, default="观察", index=True)
    stock_verdict: Mapped[str] = mapped_column(String(16), nullable=False, default="观察", index=True)
    timing_verdict: Mapped[str] = mapped_column(String(16), nullable=False, default="观察", index=True)
    overall_verdict: Mapped[str] = mapped_column(String(16), nullable=False, default="观察", index=True)
    pricing_status: Mapped[str] = mapped_column(String(24), nullable=False, default="部分定价", index=True)
    priced_in: Mapped[str | None] = mapped_column(Text, nullable=True)
    not_priced_in: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_signal: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalidation: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_company_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    phase_entered_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    segments: Mapped[list["IndustryChainSegment"]] = relationship(back_populates="chain", cascade="all, delete-orphan")
    companies: Mapped[list["IndustryChainCompany"]] = relationship(back_populates="chain", cascade="all, delete-orphan")
    evidence: Mapped[list["IndustryChainEvidence"]] = relationship(back_populates="chain", cascade="all, delete-orphan")
    tasks: Mapped[list["IndustryChainTask"]] = relationship(back_populates="chain", cascade="all, delete-orphan")
    opportunity_links: Mapped[list["IndustryChainOpportunityLink"]] = relationship(back_populates="chain", cascade="all, delete-orphan")


class IndustryChainSegment(Base):
    __tablename__ = "industry_chain_segments"
    __table_args__ = (UniqueConstraint("chain_id", "name", name="uq_industry_chain_segment_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    chain: Mapped[IndustryChain] = relationship(back_populates="segments")
    companies: Mapped[list["IndustryChainCompany"]] = relationship(back_populates="segment")
    opportunity_links: Mapped[list["IndustryChainOpportunityLink"]] = relationship(back_populates="segment")


class IndustryChainCompany(Base):
    __tablename__ = "industry_chain_companies"
    __table_args__ = (UniqueConstraint("chain_id", "full_code", name="uq_industry_chain_company_full_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("industry_chain_segments.id", ondelete="SET NULL"), nullable=True, index=True)
    code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    exchange: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    full_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="A股", index=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[str | None] = mapped_column(Text, nullable=True)
    elasticity_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50, index=True)
    tracking_status: Mapped[str] = mapped_column(String(40), nullable=False, default="观察", index=True)
    core_logic: Mapped[str | None] = mapped_column(Text, nullable=True)
    main_risk: Mapped[str | None] = mapped_column(Text, nullable=True)
    company_standing: Mapped[str | None] = mapped_column(Text, nullable=True)
    benefit_directness: Mapped[str | None] = mapped_column(String(24), nullable=True)
    profit_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False, default="未验证", index=True)
    pricing_status: Mapped[str] = mapped_column(String(24), nullable=False, default="部分定价", index=True)
    is_global_leader: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    is_domestic_alternative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    primary_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    market_implied_expectation: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_based_expectation: Mapped[str | None] = mapped_column(Text, nullable=True)
    expectation_gap_status: Mapped[str] = mapped_column(String(24), nullable=False, default="无法判断", index=True)
    expectation_gap_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expectation_trigger: Mapped[str | None] = mapped_column(Text, nullable=True)
    expectation_invalidation: Mapped[str | None] = mapped_column(Text, nullable=True)
    expectation_as_of: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    expectation_anchor_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    expectation_evidence_growth_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    expectation_evidence_acceleration_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    chain: Mapped[IndustryChain] = relationship(back_populates="companies")
    segment: Mapped[IndustryChainSegment | None] = relationship(back_populates="companies")
    evidence: Mapped[list["IndustryChainEvidence"]] = relationship(back_populates="company")
    tasks: Mapped[list["IndustryChainTask"]] = relationship(back_populates="company")
    opportunity_links: Mapped[list["IndustryChainOpportunityLink"]] = relationship(back_populates="company")


class CompanyExpectationSnapshot(Base):
    """Auditable consensus and reverse-pricing snapshot for an industry company.

    The structured payloads stay as JSON text because estimate providers expose
    different fields by market.  Keeping a dated snapshot lets us track future
    consensus revisions instead of silently replacing yesterday's expectation.
    """

    __tablename__ = "company_expectation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "provider",
            "snapshot_date",
            name="uq_company_expectation_snapshot_day",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("industry_chain_companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    consensus_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    valuation_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    backtest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryExpectationSnapshot(Base):
    """Dated aggregation of company consensus for one industry chain."""

    __tablename__ = "industry_expectation_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "chain_id",
            "snapshot_date",
            name="uq_industry_expectation_snapshot_day",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(
        ForeignKey("industry_chains.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready", index=True)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryChainEvidence(Base):
    __tablename__ = "industry_chain_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("industry_chain_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact_level: Mapped[str] = mapped_column(String(24), nullable=False, default="中", index=True)
    source_tier: Mapped[str] = mapped_column(String(24), nullable=False, default="官方硬证据", index=True)
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False, default="单一来源", index=True)
    evidence_state: Mapped[str] = mapped_column(String(24), nullable=False, default="有效", index=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    conflict_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    chain: Mapped[IndustryChain] = relationship(back_populates="evidence")
    company: Mapped[IndustryChainCompany | None] = relationship(back_populates="evidence")


class IndustryTrendNodeSource(Base):
    __tablename__ = "industry_trend_node_sources"
    __table_args__ = (UniqueConstraint("node_id", "evidence_id", name="uq_industry_trend_node_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("industry_trend_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("industry_chain_evidence.id", ondelete="CASCADE"), nullable=False, index=True)
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False, default="支持", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class IndustryChainTask(Base):
    __tablename__ = "industry_chain_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("industry_chain_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("industry_trend_nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(24), nullable=False, default="中", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="待验证", index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    chain: Mapped[IndustryChain] = relationship(back_populates="tasks")
    company: Mapped[IndustryChainCompany | None] = relationship(back_populates="tasks")


class IndustryChainOpportunityLink(Base):
    __tablename__ = "industry_chain_opportunity_links"
    __table_args__ = (UniqueConstraint("opportunity_item_id", name="uq_industry_chain_opportunity_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    segment_id: Mapped[int | None] = mapped_column(ForeignKey("industry_chain_segments.id", ondelete="SET NULL"), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("industry_chain_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    opportunity_item_id: Mapped[int] = mapped_column(ForeignKey("market_opportunity_items.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    chain: Mapped[IndustryChain] = relationship(back_populates="opportunity_links")
    segment: Mapped[IndustryChainSegment | None] = relationship(back_populates="opportunity_links")
    company: Mapped[IndustryChainCompany | None] = relationship(back_populates="opportunity_links")
    opportunity_item: Mapped[MarketOpportunityItem] = relationship()


class IndustryTrendNode(Base):
    __tablename__ = "industry_trend_nodes"
    __table_args__ = (UniqueConstraint("chain_id", "name", name="uq_industry_trend_node_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False, default="光互联产品", index=True)
    plain_explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_flow: Mapped[str | None] = mapped_column(Text, nullable=True)
    watch_signal: Mapped[str | None] = mapped_column(Text, nullable=True)
    maturity_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    market_space: Mapped[str | None] = mapped_column(Text, nullable=True)
    tech_barrier: Mapped[str | None] = mapped_column(Text, nullable=True)
    competition: Mapped[str | None] = mapped_column(Text, nullable=True)
    profit_elasticity: Mapped[str | None] = mapped_column(String(24), nullable=True)
    localization: Mapped[str | None] = mapped_column(String(24), nullable=True)
    investment_importance: Mapped[str | None] = mapped_column(String(24), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryTrendEdge(Base):
    __tablename__ = "industry_trend_edges"
    __table_args__ = (UniqueConstraint("chain_id", "from_node_id", "to_node_id", name="uq_industry_trend_edge"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    from_node_id: Mapped[int] = mapped_column(ForeignKey("industry_trend_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    to_node_id: Mapped[int] = mapped_column(ForeignKey("industry_trend_nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class IndustryTrendUpdate(Base):
    __tablename__ = "industry_trend_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    update_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_verification: Mapped[str | None] = mapped_column(Text, nullable=True)
    affects_phase: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    affects_decision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    phase_suggestion: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decision_suggestion_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    causal_stage: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    evidence_type: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    signal_status: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    buyer_group: Mapped[str | None] = mapped_column(String(160), nullable=True)
    market_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    sell_pressure: Mapped[str | None] = mapped_column(String(16), nullable=True)
    counter_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryTrendCatalyst(Base):
    __tablename__ = "industry_trend_catalysts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    event_name: Mapped[str] = mapped_column(String(240), nullable=False)
    expected_time: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, default="其他", index=True)
    impact_node_id: Mapped[int | None] = mapped_column(ForeignKey("industry_trend_nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    impact_company_id: Mapped[int | None] = mapped_column(ForeignKey("industry_chain_companies.id", ondelete="SET NULL"), nullable=True, index=True)
    importance: Mapped[str] = mapped_column(String(16), nullable=False, default="中", index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="预期", index=True)
    impact: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryTrendDraft(Base):
    __tablename__ = "industry_trend_drafts"
    __table_args__ = (UniqueConstraint("chain_id", name="uq_industry_trend_draft_chain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    draft_type: Mapped[str] = mapped_column(String(24), nullable=False, default="initial", index=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="codex_web", index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryTrendMaterial(Base):
    """New research material waiting to be reconciled into a formal industry thesis."""

    __tablename__ = "industry_trend_materials"
    __table_args__ = (
        UniqueConstraint("chain_id", "material_key", name="uq_industry_trend_material_key"),
        Index("ix_industry_trend_material_queue", "chain_id", "status", "material_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    material_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="本地对话", index=True)
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    material_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False, default="新增证据", index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="待处理", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryTrendResearchSetting(Base):
    __tablename__ = "industry_trend_research_settings"
    __table_args__ = (UniqueConstraint("chain_id", name="uq_industry_trend_research_setting_chain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    priority_nodes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    priority_companies_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_preferences_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    excluded_keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=24000)
    allow_new_nodes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_new_companies: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    draft_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_researched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryTrendVersion(Base):
    __tablename__ = "industry_trend_versions"
    __table_args__ = (UniqueConstraint("chain_id", "revision", name="uq_industry_trend_version_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)


class IndustryTrendGenerationJob(Base):
    __tablename__ = "industry_trend_generation_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(24), nullable=False, default="initial", index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", index=True)
    phase: Mapped[str] = mapped_column(String(80), nullable=False, default="等待开始")
    input_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    process_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InformationScreeningBatch(Base):
    __tablename__ = "information_screening_batches"
    __table_args__ = (
        UniqueConstraint("batch_key", name="uq_information_screening_batch_key"),
        Index("ix_information_screening_batch_window", "window_end", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    source_scope_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    read_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deduplicated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conversation_thread_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    items: Mapped[list["InformationScreeningItem"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class InformationScreeningItem(Base):
    __tablename__ = "information_screening_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "item_key", name="uq_information_screening_batch_item"),
        Index("ix_information_screening_item_bucket", "batch_id", "bucket", "importance", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("information_screening_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="other", index=True)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_urls_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    bucket: Mapped[str] = mapped_column(String(32), nullable=False, default="eye_catching", index=True)
    is_top: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3, index=True)
    marginal_change: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unverified", index=True)
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    related_sectors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    related_stocks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    price_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    price_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    validation_points_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    invalidation_conditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    filter_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recovery_condition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    official_check_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_required", index=True)
    official_check_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    official_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    official_stock_codes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    official_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_chain_id: Mapped[int | None] = mapped_column(
        ForeignKey("industry_chains.id", ondelete="SET NULL"), nullable=True, index=True
    )
    conversation_thread_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    batch: Mapped[InformationScreeningBatch] = relationship(back_populates="items")


class InformationScreeningFeedback(Base):
    __tablename__ = "information_screening_feedback"
    __table_args__ = (
        Index("ix_information_screening_feedback_key_action", "item_key", "action"),
        Index("ix_information_screening_feedback_reason_created", "reason_category", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    batch_key: Mapped[str] = mapped_column(String(180), nullable=False, default="", index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="other", index=True)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    bucket: Mapped[str] = mapped_column(String(32), nullable=False, default="eye_catching", index=True)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unverified", index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_delete", index=True)
    reason_category: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    conversation_thread_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class InvestmentDecisionEvent(Base):
    """Frozen paper-decision made from an information-screening item."""

    __tablename__ = "investment_decision_events"
    __table_args__ = (
        UniqueConstraint("information_item_id", name="uq_investment_decision_information_item"),
        UniqueConstraint("decision_key", name="uq_investment_decision_key"),
        Index("ix_investment_decision_status_signal", "decision_code", "execution_status", "signal_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    information_item_id: Mapped[int] = mapped_column(
        ForeignKey("information_screening_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    information_item_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    information_title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="other", index=True)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decision_code: Mapped[str] = mapped_column(String(1), nullable=False, index=True)
    primary_stock_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    primary_stock_name: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    primary_full_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    alternatives_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    direction_verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stock_verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pricing_verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    why_best: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trigger_conditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    invalidation_conditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    planned_horizon: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    benchmark_code: Mapped[str] = mapped_column(String(16), nullable=False, default="SH000300")
    cost_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting_entry", index=True)
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class IndustryTrendDecision(Base):
    """Immutable paper-decision frozen from an industry trend workspace."""

    __tablename__ = "industry_trend_decisions"
    __table_args__ = (
        UniqueConstraint("decision_key", name="uq_industry_trend_decision_key"),
        Index("ix_industry_trend_decision_status_signal", "decision_code", "execution_status", "signal_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    chain_id: Mapped[int] = mapped_column(ForeignKey("industry_chains.id", ondelete="RESTRICT"), nullable=False, index=True)
    chain_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decision_code: Mapped[str] = mapped_column(String(1), nullable=False, index=True)
    primary_stock_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    primary_stock_name: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    primary_full_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    alternatives_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    direction_verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stock_verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    timing_verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pricing_verdict: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    why_best: Mapped[str] = mapped_column(Text, nullable=False, default="")
    trigger_conditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    invalidation_conditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    planned_horizon: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    benchmark_code: Mapped[str] = mapped_column(String(16), nullable=False, default="SH000300")
    cost_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting_entry", index=True)
    entry_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class WatchlistAnnouncementStock(Base):
    __tablename__ = "watchlist_announcement_stocks"
    __table_args__ = (UniqueConstraint("full_code", name="uq_watch_ann_stock_full_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    org_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    push_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    items: Mapped[list["WatchlistAnnouncementItem"]] = relationship(back_populates="watch_stock")


class WatchlistAnnouncementItem(Base):
    __tablename__ = "watchlist_announcement_items"
    __table_args__ = (UniqueConstraint("source_type", "external_id", name="uq_watch_ann_item_source_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_stock_id: Mapped[int | None] = mapped_column(
        ForeignKey("watchlist_announcement_stocks.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    importance_level: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_ai")
    should_push: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_status: Mapped[str] = mapped_column(String(40), nullable=False, default="not_configured")
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    push_status: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_only")
    push_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    watch_stock: Mapped[WatchlistAnnouncementStock | None] = relationship(back_populates="items")
    push_logs: Mapped[list["WatchlistAnnouncementPushLog"]] = relationship(back_populates="item", cascade="all, delete-orphan")


class WatchlistAnnouncementCrawlLog(Base):
    __tablename__ = "watchlist_announcement_crawl_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    full_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    saved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ignored_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class WatchlistAnnouncementPushLog(Base):
    __tablename__ = "watchlist_announcement_push_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("watchlist_announcement_items.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    full_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    group_name: Mapped[str] = mapped_column(String(40), nullable=False, default="自选股公告")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    item: Mapped[WatchlistAnnouncementItem | None] = relationship(back_populates="push_logs")


class ExchangeAnnouncementPushLog(Base):
    __tablename__ = "exchange_announcement_push_logs"
    __table_args__ = (UniqueConstraint("announcement_key", name="uq_exchange_ann_push_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    group_name: Mapped[str] = mapped_column(String(40), nullable=False, default="交易所公告")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    asset_label: Mapped[str] = mapped_column(String(40), nullable=False, default="未分类")
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class ExchangeAnnouncementTimeline(Base):
    __tablename__ = "exchange_announcement_timelines"
    __table_args__ = (
        UniqueConstraint(
            "announcement_key",
            "symbol",
            name="uq_exchange_announcement_timeline_key_symbol",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    market_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    astro_registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_direct_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    astro_status: Mapped[str] = mapped_column(String(48), nullable=False, default="waiting_market")
    astro_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class ExchangeAnnouncementChangeLog(Base):
    __tablename__ = "exchange_announcement_change_logs"
    __table_args__ = (
        Index("ix_exchange_announcement_change_key_created", "announcement_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    change_type: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, index=True, nullable=False
    )


class ExchangeDelistingOpportunityWatch(Base):
    __tablename__ = "exchange_delisting_opportunity_watches"
    __table_args__ = (
        UniqueConstraint("announcement_key", "symbol", name="uq_exchange_delisting_watch_announcement_symbol"),
        Index("ix_exchange_delisting_watch_active_expires", "enabled", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    announcement_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    source_market_type: Mapped[str] = mapped_column(String(16), nullable=False)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    routes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="watch")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class ExchangeDelistingOpportunityMute(Base):
    __tablename__ = "exchange_delisting_opportunity_mutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class ExchangeDelistingOpportunityPairExclusion(Base):
    __tablename__ = "exchange_delisting_opportunity_pair_exclusions"
    __table_args__ = (
        UniqueConstraint(
            "watch_id",
            "pair_key",
            name="uq_exchange_delisting_pair_exclusion",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_delisting_opportunity_watches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    pair_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now_utc,
        nullable=False,
    )


class ExchangeDelistingOpportunitySnapshot(Base):
    __tablename__ = "exchange_delisting_opportunity_snapshots"
    __table_args__ = (
        Index("ix_exchange_delisting_snapshot_watch_pair_created", "watch_id", "pair_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_delisting_opportunity_watches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pair_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    pair_type: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    left_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    left_market_type: Mapped[str] = mapped_column(String(16), nullable=False)
    right_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    right_market_type: Mapped[str] = mapped_column(String(16), nullable=False)
    left_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    left_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    reference_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_left_buy_right_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_right_buy_left_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_executable_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(200), nullable=True)
    left_volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="watch")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class ExchangeDelistingOpportunityAlertLog(Base):
    __tablename__ = "exchange_delisting_opportunity_alert_logs"
    __table_args__ = (
        Index("ix_exchange_delisting_alert_watch_pair_created", "watch_id", "pair_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(
        ForeignKey("exchange_delisting_opportunity_watches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pair_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class WatchlistAnnouncementAiSetting(Base):
    __tablename__ = "watchlist_announcement_ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="deepseek")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="deepseek-chat")
    base_url: Mapped[str] = mapped_column(Text, nullable=False, default="https://api.deepseek.com")
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class EditedNewsItem(Base):
    __tablename__ = "edited_news_items"
    __table_args__ = (
        UniqueConstraint("source_url", name="uq_edited_news_source_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_category: Mapped[str] = mapped_column(String(40), nullable=False, default="global")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    impact_level: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    impact_path: Mapped[str] = mapped_column(Text, nullable=False)
    related_sectors: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    related_stocks: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    topic_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    crawled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    pushed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    push_status: Mapped[str] = mapped_column(String(40), default="manual_only", nullable=False)
    push_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class EditedNewsCrawlLog(Base):
    __tablename__ = "edited_news_crawl_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    selected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ignored_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class EditedNewsFeedback(Base):
    __tablename__ = "edited_news_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    news_item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_category: Mapped[str] = mapped_column(String(40), nullable=False, default="global")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    impact_level: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    related_sectors: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class CryptoWatchItem(Base):
    __tablename__ = "crypto_watch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    left_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    right_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    left_market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="futures")
    right_market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="futures")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    snapshots: Mapped[list["CryptoSpreadSnapshot"]] = relationship(back_populates="watch_item", cascade="all, delete-orphan")
    push_rule: Mapped["CryptoPushRule | None"] = relationship(back_populates="watch_item", cascade="all, delete-orphan")
    push_logs: Mapped[list["CryptoPushLog"]] = relationship(back_populates="watch_item", cascade="all, delete-orphan")


class CryptoSpreadSnapshot(Base):
    __tablename__ = "crypto_spread_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_item_id: Mapped[int] = mapped_column(ForeignKey("crypto_watch_items.id", ondelete="CASCADE"), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    left_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    right_exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    left_market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="futures")
    right_market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="futures")
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    left_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    left_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    left_mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    left_index_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_index_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    left_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    left_premium_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_premium_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    left_next_funding_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    right_next_funding_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    watch_item: Mapped[CryptoWatchItem] = relationship(back_populates="snapshots")


class CryptoMarketQuoteSnapshot(Base):
    __tablename__ = "crypto_market_quote_snapshots"
    __table_args__ = (
        Index(
            "ix_crypto_market_quote_pair_history",
            "symbol",
            "exchange",
            "market_type",
            "source",
            "batch_time",
        ),
        Index(
            "ix_crypto_market_quote_negative_funding_scan",
            "market_type",
            "status",
            "exchange",
            "batch_time",
            "funding_rate",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    index_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    premium_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_fund: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="exchange_api")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class CryptoBoardSetting(Base):
    __tablename__ = "crypto_board_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled_exchanges_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class CryptoSymbolMapping(Base):
    __tablename__ = "crypto_symbol_mappings"
    __table_args__ = (
        UniqueConstraint("input_symbol", "exchange", "market_type", name="uq_crypto_symbol_mapping_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    input_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    market_type: Mapped[str] = mapped_column(String(16), nullable=False, default="futures", index=True)
    mapped_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    price_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class CryptoPushRule(Base):
    __tablename__ = "crypto_push_rules"
    __table_args__ = (UniqueConstraint("watch_item_id", name="uq_crypto_push_rule_watch_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_item_id: Mapped[int] = mapped_column(ForeignKey("crypto_watch_items.id", ondelete="CASCADE"), index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    open_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    premium_diff_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    watch_item: Mapped[CryptoWatchItem] = relationship(back_populates="push_rule")
    logs: Mapped[list["CryptoPushLog"]] = relationship(back_populates="rule", cascade="all, delete-orphan")


class CryptoPushLog(Base):
    __tablename__ = "crypto_push_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_item_id: Mapped[int] = mapped_column(ForeignKey("crypto_watch_items.id", ondelete="CASCADE"), index=True, nullable=False)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("crypto_push_rules.id", ondelete="SET NULL"), index=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    premium_diff_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    watch_item: Mapped[CryptoWatchItem] = relationship(back_populates="push_logs")
    rule: Mapped[CryptoPushRule | None] = relationship(back_populates="logs")


class CryptoFsSignalLog(Base):
    __tablename__ = "crypto_fs_signal_logs"
    __table_args__ = (
        Index("ix_crypto_fs_signal_key_created", "signal_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    futures_exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    spot_exchange: Mapped[str] = mapped_column(String(16), nullable=False, default="bg", index=True)
    funding_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    borrow_period_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    borrowable_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class CryptoFsObservationLog(Base):
    __tablename__ = "crypto_fs_observation_logs"
    __table_args__ = (
        Index("ix_crypto_fs_observation_scope_created", "symbol", "futures_exchange", "spot_exchange", "created_at"),
        Index("ix_crypto_fs_observation_batch_created", "batch_time", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    futures_exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    spot_exchange: Mapped[str] = mapped_column(String(16), nullable=False, default="bg", index=True)
    funding_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    premium_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    potential_type: Mapped[str] = mapped_column(String(32), nullable=False, default="current_negative", index=True)
    inventory_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    executable_borrow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    borrowable_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    borrowable_value_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_borrow_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    basis_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="watch", index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class CryptoFsRuntimeLog(Base):
    __tablename__ = "crypto_fs_runtime_logs"
    __table_args__ = (
        Index("ix_crypto_fs_runtime_scan_created", "scan_id", "created_at"),
        Index("ix_crypto_fs_runtime_level_created", "level", "created_at"),
        Index("ix_crypto_fs_runtime_symbol_created", "symbol", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info", index=True)
    stage: Mapped[str] = mapped_column(String(48), nullable=False, default="scan")
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    futures_exchange: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    spot_exchange: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class CryptoCompoundOpenSignalLog(Base):
    __tablename__ = "crypto_compound_open_signal_logs"
    __table_args__ = (
        Index("ix_crypto_compound_open_signal_key_created", "signal_key", "created_at"),
        Index("ix_crypto_compound_open_signal_scope_created", "strategy", "symbol", "left_exchange", "right_exchange", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    left_exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    right_exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    left_market_type: Mapped[str] = mapped_column(String(16), nullable=False)
    right_market_type: Mapped[str] = mapped_column(String(16), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    open_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_jump_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_diff_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    premium_diff_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    left_volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    right_volume_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_30m_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_30m_convergence_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_30m_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_60m_spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_60m_convergence_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_60m_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open", index=True)
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    push_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    push_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class CryptoBorrowWatchItem(Base):
    __tablename__ = "crypto_borrow_watch_items"
    __table_args__ = (UniqueConstraint("symbol", name="uq_crypto_borrow_watch_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchanges_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    logs: Mapped[list["CryptoBorrowWatchLog"]] = relationship(back_populates="watch_item", cascade="all, delete-orphan")


class CryptoFundingCapWatchItem(Base):
    __tablename__ = "crypto_funding_cap_watch_items"
    __table_args__ = (UniqueConstraint("symbol", name="uq_crypto_funding_cap_watch_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchanges_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='["bn","by","gt","okx","bg","as"]',
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    snapshots: Mapped[list["CryptoFundingCapSnapshot"]] = relationship(
        back_populates="watch_item",
        cascade="all, delete-orphan",
    )
    events: Mapped[list["CryptoFundingCapEvent"]] = relationship(
        back_populates="watch_item",
        cascade="all, delete-orphan",
    )


class CryptoFundingCapSnapshot(Base):
    __tablename__ = "crypto_funding_cap_snapshots"
    __table_args__ = (
        UniqueConstraint("watch_item_id", "exchange", name="uq_crypto_funding_cap_snapshot_route"),
        Index("ix_crypto_funding_cap_snapshot_symbol_exchange", "symbol", "exchange"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_item_id: Mapped[int] = mapped_column(
        ForeignKey("crypto_funding_cap_watch_items.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_interval_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)

    watch_item: Mapped[CryptoFundingCapWatchItem] = relationship(back_populates="snapshots")


class CryptoFundingCapEvent(Base):
    __tablename__ = "crypto_funding_cap_events"
    __table_args__ = (
        Index("ix_crypto_funding_cap_event_symbol_exchange_created", "symbol", "exchange", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_item_id: Mapped[int] = mapped_column(
        ForeignKey("crypto_funding_cap_watch_items.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    previous_max_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_max_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_min_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_min_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_funding_interval_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_interval_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    push_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    push_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    watch_item: Mapped[CryptoFundingCapWatchItem] = relationship(back_populates="events")


class CryptoFundingFormationWatchItem(Base):
    __tablename__ = "crypto_funding_formation_watch_items"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", name="uq_crypto_funding_formation_watch_route"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class CryptoFundingFormationPredictionLog(Base):
    __tablename__ = "crypto_funding_formation_prediction_logs"
    __table_args__ = (
        UniqueConstraint(
            "exchange",
            "symbol",
            "settlement_time",
            "checkpoint_minutes",
            name="uq_crypto_funding_prediction_checkpoint",
        ),
        Index(
            "ix_crypto_funding_prediction_review",
            "checkpoint_minutes",
            "evaluation_status",
            "settlement_time",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    cycle_start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settlement_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    checkpoint_minutes: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    lead_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    system_predicted_rate: Mapped[float] = mapped_column(Float, nullable=False)
    exchange_predicted_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    predicted_average_premium_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_premium_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_premium_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    weighted_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    prediction_method: Mapped[str] = mapped_column(String(80), nullable=False)
    prediction_model_version: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    formula_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    calculation_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    shadow_formula_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    shadow_predicted_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    shadow_absolute_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    shadow_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    funding_interval_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok")
    actual_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_funding_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    absolute_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    signed_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    success_tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exchange_absolute_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    exchange_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    evaluation_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    evaluation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_evaluation_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class CryptoBorrowWatchLog(Base):
    __tablename__ = "crypto_borrow_watch_logs"
    __table_args__ = (
        Index("ix_crypto_borrow_watch_symbol_exchange_created", "symbol", "exchange", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_item_id: Mapped[int] = mapped_column(ForeignKey("crypto_borrow_watch_items.id", ondelete="CASCADE"), index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    can_borrow: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    borrowable_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    hourly_borrow_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_borrow_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    push_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    push_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)

    watch_item: Mapped[CryptoBorrowWatchItem] = relationship(back_populates="logs")


class CryptoCoinStatusLog(Base):
    __tablename__ = "crypto_coin_status_logs"
    __table_args__ = (
        Index("ix_crypto_coin_status_symbol_exchange_created", "symbol", "exchange", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    deposit_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    withdraw_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    chains_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    borrow_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    borrow_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    can_borrow: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    borrowable_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    hourly_borrow_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_borrow_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    index_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    index_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    index_components_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class CryptoIndexComponentChangeLog(Base):
    __tablename__ = "crypto_index_component_change_logs"
    __table_args__ = (
        Index("ix_crypto_index_component_change_symbol_created", "symbol", "created_at"),
        Index("ix_crypto_index_component_change_scope", "symbol", "exchange", "component", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    old_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    new_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    diff: Mapped[float | None] = mapped_column(Float, nullable=True)
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    push_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    push_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class CryptoMonitorEvent(Base):
    __tablename__ = "crypto_monitor_events"
    __table_args__ = (
        Index("ix_crypto_monitor_event_symbol_created", "symbol", "created_at"),
        Index("ix_crypto_monitor_event_scope_created", "symbol", "exchange", "event_type", "created_at"),
        Index("ix_crypto_monitor_event_ack_created", "acknowledged_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info", index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    push_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    push_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class MarketReviewUniverseItem(Base):
    __tablename__ = "market_review_universe_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    theme: Mapped[str] = mapped_column(String(80), nullable=False, default="AI叙事")
    role: Mapped[str] = mapped_column(String(120), nullable=False, default="观察标的")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FutureEvent(Base):
    __tablename__ = "future_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_future_event_key"),
        Index("ix_future_event_date_stage", "start_date", "stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False, default="其他", index=True)
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="B", index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="clue", index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exact_time: Mapped[str | None] = mapped_column(String(80), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    source_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unverified")
    source_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_urls_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    market_scope: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    stock_mappings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    impact_chain: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priced_in_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    price_expression: Mapped[str] = mapped_column(Text, nullable=False, default="")
    validation_points_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    invalidation_conditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    stage_history_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    conversation_thread_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class FutureEventFeedback(Base):
    __tablename__ = "future_event_feedback"
    __table_args__ = (
        Index("ix_future_event_feedback_key_action", "event_key", "action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False, default="其他", index=True)
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="B", index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="clue")
    action: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_delete", index=True)
    reason_category: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    conversation_thread_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class ChangePricingAnalysis(Base):
    __tablename__ = "change_pricing_analyses"
    __table_args__ = (
        UniqueConstraint("analysis_key", name="uq_change_pricing_analysis_key"),
        Index("ix_change_pricing_subject_updated", "subject_code", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    subject_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    subject_code: Mapped[str] = mapped_column(String(40), nullable=False, default="", index=True)
    change_title: Mapped[str] = mapped_column(String(280), nullable=False)
    change_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    source_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="B")
    source_label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    source_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_urls_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    fundamental_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    freshness_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    chain_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    evidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    chart_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    excess_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fair_value_uplift: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    earnings_revision: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    valuation_percentile: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    crowding: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    chain_diffusion: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    days_elapsed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reflected_items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    unreflected_items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    validation_points_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    invalidation_conditions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conversation_thread_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class ChangePricingFeedback(Base):
    __tablename__ = "change_pricing_feedback"
    __table_args__ = (
        Index("ix_change_pricing_feedback_key_action", "analysis_key", "action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_key: Mapped[str] = mapped_column(String(180), nullable=False, index=True)
    subject_name: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_code: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_delete", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    conversation_thread_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)


class MarketReviewMaterial(Base):
    __tablename__ = "market_review_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    source_name: Mapped[str] = mapped_column(String(120), nullable=False)
    market: Mapped[str] = mapped_column(String(24), nullable=False, default="both", index=True)
    theme: Mapped[str] = mapped_column(String(80), nullable=False, default="AI叙事")
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ok")
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class MarketReviewReport(Base):
    __tablename__ = "market_review_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="manual_only")
    markdown_path: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ai_status: Mapped[str] = mapped_column(String(40), nullable=False, default="not_configured")
    source_status_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class MarketReviewAiSetting(Base):
    __tablename__ = "market_review_ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="deepseek")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="deepseek-chat")
    base_url: Mapped[str] = mapped_column(Text, nullable=False, default="https://api.deepseek.com")
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)
