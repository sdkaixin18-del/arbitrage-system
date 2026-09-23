from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class TargetCreate(BaseModel):
    profileUrl: HttpUrl


class TargetUpdate(BaseModel):
    enablePortfolio: bool | None = None
    enablePost: bool | None = None
    enablePortfolioPush: bool | None = None
    enablePostPush: bool | None = None


class TargetOut(BaseModel):
    id: str
    targetDbId: int | None = None
    nickname: str
    avatarUrl: str = ""
    userId: str
    profileUrl: str
    portfolioUrl: str
    enablePortfolio: bool
    enablePost: bool
    enablePortfolioPush: bool
    enablePostPush: bool
    createdAt: int
    updatedAt: int
    last_home_feed_status: str = "not_configured"
    last_home_feed_message: str | None = None
    last_home_feed_at: datetime | None = None
    last_watchlist_status: str = "not_configured"
    last_watchlist_message: str | None = None
    last_watchlist_at: datetime | None = None


class XueqiuPostOut(BaseModel):
    id: int
    target_id: int
    author_name: str
    author_user_id: str | None
    content: str
    source_url: str | None
    published_at: datetime | None
    crawled_at: datetime

    model_config = {"from_attributes": True}


class WatchlistEventOut(BaseModel):
    id: int
    target_id: int
    target_nickname: str
    event_type: str
    stock_code: str
    stock_name: str
    exchange: str
    full_code: str
    price: float | None
    source_url: str
    push_status: str
    push_message: str | None
    created_at: datetime


class PushLogOut(BaseModel):
    id: int
    target_id: int | None
    group_name: str
    event_type: str
    title: str
    body: str
    link: str | None
    status: str
    message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CrawlLogOut(BaseModel):
    id: int
    scope: str
    target_id: int | None
    status: str
    message: str | None
    matched_count: int
    ignored_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class PortfolioStockStat(BaseModel):
    target_id: int
    nickname: str
    stock_code: str
    stock_name: str
    exchange: str
    full_code: str
    first_price: float | None
    latest_price: float | None
    profit_per_share: float | None
    first_seen_at: datetime | None
    latest_seen_at: datetime | None


class PortfolioUserStat(BaseModel):
    target_id: int
    nickname: str
    success_count: int
    total_count: int
    success_rate: float | None
    stocks: list[PortfolioStockStat]


class PortfolioWinRateSummary(BaseModel):
    target_id: int
    nickname: str
    window_days: int
    total_count: int
    closed_count: int
    open_count: int
    closed_win_count: int
    closed_loss_count: int
    open_win_count: int
    open_loss_count: int
    closed_win_rate: float | None
    open_win_rate: float | None
    average_return_pct: float | None
    profit_stock_names: list[str]
    loss_stock_names: list[str]


class PortfolioWinRateItem(BaseModel):
    target_id: int
    nickname: str
    stock_code: str
    stock_name: str
    full_code: str
    start_at: datetime | None
    start_price: float | None
    end_at: datetime | None
    end_price: float | None
    status: str
    return_pct: float | None


class PortfolioWinRateOverview(BaseModel):
    summaries: list[PortfolioWinRateSummary]
    recent_items: list[PortfolioWinRateItem]


class XueqiuRecommendationCreate(BaseModel):
    target_id: int
    query: str = Field(min_length=1, max_length=120)


class XueqiuRecommendationItem(BaseModel):
    id: int
    target_id: int
    nickname: str
    stock_code: str
    stock_name: str
    exchange: str
    full_code: str
    source_type: str
    source_post_id: int | None
    source_url: str | None
    source_excerpt: str | None
    start_at: datetime | None
    start_price: float | None
    end_at: datetime | None
    end_price: float | None
    status: str
    return_pct: float | None


class XueqiuRecommendationSummary(BaseModel):
    target_id: int
    nickname: str
    window_days: int
    total_count: int
    closed_count: int
    open_count: int
    closed_win_count: int
    closed_loss_count: int
    open_win_count: int
    open_loss_count: int
    closed_win_rate: float | None
    open_win_rate: float | None
    average_return_pct: float | None
    profit_stock_names: list[str]
    loss_stock_names: list[str]


class XueqiuRecommendedStocksOverview(BaseModel):
    summaries: list[XueqiuRecommendationSummary]
    recent_items: list[XueqiuRecommendationItem]


class XueqiuOverview(BaseModel):
    status: str
    authorization_status: str
    bark_status: str
    profile_dir: str
    targets: list[TargetOut]
    recent_posts: list[XueqiuPostOut]
    recent_events: list[WatchlistEventOut]
    push_logs: list[PushLogOut]
    crawl_logs: list[CrawlLogOut]
    portfolio_stats: list[PortfolioUserStat]
    portfolio_win_rate: PortfolioWinRateOverview
    recommended_stocks: XueqiuRecommendedStocksOverview


class NetworkMessageSourceOut(BaseModel):
    source_type: str
    source_name: str
    status: str
    message: str | None
    updated_at: datetime | None


class NetworkMessageItemOut(BaseModel):
    id: str
    source_type: str
    source_name: str
    message_type: str
    matched_stocks: list[str] = Field(default_factory=list)
    author: str | None
    title: str
    content: str
    image_count: int = 0
    image_status: str | None = None
    image_text: str | None = None
    image_message: str | None = None
    link: str | None
    status: str
    created_at: datetime


class NetworkMessagesOverview(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    message: str
    sources: list[NetworkMessageSourceOut]
    items: list[NetworkMessageItemOut]


class CrawlResult(BaseModel):
    status: str
    message: str | None = None
    matched_count: int = 0
    ignored_count: int = 0
    target_id: int | None = None


class FactorTagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=32)


class FactorTagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=32)


class FactorTagOut(BaseModel):
    id: int
    name: str
    color: str | None
    stock_count: int
    created_at: datetime
    updated_at: datetime | None
    is_generic_direction: bool = False
    trade_role: str = "candidate_mainline"
    tag_kind: str = "待验证"


class FactorTagBulkCreate(BaseModel):
    names: list[str] = Field(min_length=1, max_length=240)


class FactorTagCandidateRunRequest(BaseModel):
    lookback_days: int = Field(default=30, ge=5, le=90)
    limit: int = Field(default=120, ge=20, le=240)


class FactorTagCandidateRunOut(BaseModel):
    id: int
    status: str
    message: str | None
    lookback_days: int
    item_count: int
    failed_count: int
    started_at: datetime
    completed_at: datetime | None


class FactorTagCandidateOut(BaseModel):
    name: str
    category: str
    direction: str
    score: float
    reason: str
    sources: list[str]
    example_stocks: list[str]
    returns: dict[str, float | int | str]


class FactorTagCandidatesResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    candidates: list[FactorTagCandidateOut]
    sources: list[dict]
    latest_run: FactorTagCandidateRunOut | None
    message: str


class FactorAutoTagRunOut(BaseModel):
    id: int
    status: str
    message: str | None
    stock_count: int
    tagged_count: int
    tag_count: int
    source_statuses: list[dict] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime | None


class FactorAutoTagResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    latest_run: FactorAutoTagRunOut | None
    ima_cache: dict = Field(default_factory=dict)
    message: str


class FactorPresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    periods: list[int] = Field(min_length=1, max_length=8)
    active: bool = False
    sort_order: int = 100


class FactorPresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    periods: list[int] | None = Field(default=None, min_length=1, max_length=8)
    sort_order: int | None = None


class FactorPresetOut(BaseModel):
    id: int
    name: str
    periods: list[int]
    active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime | None


class FactorStockTagsUpdate(BaseModel):
    tag_ids: list[int] = Field(default_factory=list)


class FactorStockStatusUpdate(BaseModel):
    status: str = Field(pattern="^(unreviewed|needs_more|done)$")
    review_reasons: list[str] = Field(default_factory=list)


class FactorStockTagOut(BaseModel):
    code: str
    name: str
    exchange: str
    full_code: str
    tags: list[FactorTagOut]
    tag_status: str
    watchlist_added: bool = False
    watchlist_enabled: bool = False
    watchlist_push_enabled: bool = False


class FactorRankingStockOut(BaseModel):
    code: str
    name: str
    exchange: str
    full_code: str
    latest_price: float | None
    change_pct: float | None
    return_pct: float
    tags: list[FactorTagOut]
    is_untagged: bool
    tag_status: str
    watchlist_added: bool = False
    watchlist_enabled: bool = False
    watchlist_push_enabled: bool = False
    updated_at: datetime | None


class FactorReviewStockOut(FactorRankingStockOut):
    reasons: list[str]
    review_reasons: list[str]


class FactorHotTagReviewStockOut(FactorRankingStockOut):
    reasons: list[str]
    suggested_tags: list[str] = Field(default_factory=list)
    amount: float | None = None


class FactorHotTagStatStockOut(BaseModel):
    full_code: str
    code: str
    name: str
    change_pct: float | None = None
    return_pct: float | None = None
    amount: float | None = None
    sources: list[str] = Field(default_factory=list)


class FactorHotTagStatOut(BaseModel):
    tag_id: int
    tag_name: str
    total_count: int
    gain_count: int
    amount_count: int
    overlap_count: int
    sample_stocks: list[FactorHotTagStatStockOut] = Field(default_factory=list)


class FactorHotTagReviewResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    message: str
    gain_limit: int
    amount_limit: int
    sample_count: int = 0
    tagged_sample_count: int = 0
    untagged_sample_count: int = 0
    tag_stats: list[FactorHotTagStatOut] = Field(default_factory=list)
    stocks: list[FactorHotTagReviewStockOut]


class FactorTaggedStocksResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    message: str
    total_count: int
    tagged_count: int
    stocks: list[FactorHotTagReviewStockOut]


class FactorTagRankingOut(BaseModel):
    tag_id: int | None
    tag_name: str
    color: str | None
    stock_count: int
    average_return_pct: float
    stock_codes: list[str]
    system: bool = False


class FactorPeriodRankingOut(BaseModel):
    period: int
    gainers: list[FactorRankingStockOut]
    losers: list[FactorRankingStockOut]
    winning_tags: list[FactorTagRankingOut]
    losing_tags: list[FactorTagRankingOut]
    board_winning_tags: list[FactorTagRankingOut] = Field(default_factory=list)
    board_losing_tags: list[FactorTagRankingOut] = Field(default_factory=list)
    coverage_count: int


class FactorEffectStyleTagOut(FactorTagRankingOut):
    periods: list[int] = Field(default_factory=list)
    evidence_days: int = 0
    evidence_level: str = "待验证"
    is_generic_direction: bool = False
    trade_role: str = "candidate_mainline"


class FactorRecentStyleTagOut(BaseModel):
    name: str
    category: str
    score: float
    description: str
    action: str
    source_tags: list[str] = Field(default_factory=list)


class FactorEffectStyleOut(BaseModel):
    rules_enabled: bool = True
    style_name: str
    summary: str
    confidence: float
    health: str
    coverage_rate: float
    coverage_label: str
    coverage_count: int
    tagged_count: int
    total_count: int
    primary_style: FactorRecentStyleTagOut | None = None
    recent_style_tags: list[FactorRecentStyleTagOut] = Field(default_factory=list)
    participation_advice: str | None = None
    avoid_advice: str | None = None
    dominant_tags: list[FactorEffectStyleTagOut] = Field(default_factory=list)
    risk_tags: list[FactorEffectStyleTagOut] = Field(default_factory=list)
    board_dominant_tags: list[FactorEffectStyleTagOut] = Field(default_factory=list)
    board_risk_tags: list[FactorEffectStyleTagOut] = Field(default_factory=list)
    tradable_mainlines: list[FactorEffectStyleTagOut] = Field(default_factory=list)
    confirmed_mainlines: list[FactorEffectStyleTagOut] = Field(default_factory=list)
    direction_only_tags: list[FactorEffectStyleTagOut] = Field(default_factory=list)
    downgraded_tags: list[FactorEffectStyleTagOut] = Field(default_factory=list)
    current_phase: dict[str, Any] = Field(default_factory=dict)
    risk_flags: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    action: str | None = None
    market: dict[str, Any] = Field(default_factory=dict)


class FactorEffectBacktestMainlineOut(BaseModel):
    style_name: str
    category: str
    days_selected: int
    avg_forward_return_pct: float | None = None
    avg_forward_excess_pct: float | None = None
    win_rate_pct: float | None = None
    avg_momentum_return_pct: float | None = None
    avg_sample_count: float | None = None
    source_tags: list[str] = Field(default_factory=list)
    latest_seen: str | None = None
    evidence_days: int = 0
    evidence_level: str = "待验证"
    is_generic_direction: bool = False
    trade_role: str = "candidate_mainline"


class FactorEffectBacktestSelectedOut(BaseModel):
    style_name: str
    category: str
    score: float
    sample_count: int
    tag_count: int
    source_tags: list[str] = Field(default_factory=list)
    avg_momentum_return_pct: float | None = None
    avg_forward_return_pct: float | None = None
    avg_forward_excess_pct: float | None = None
    evidence_days: int = 0
    evidence_level: str = "待验证"
    is_generic_direction: bool = False
    trade_role: str = "candidate_mainline"


class FactorEffectBacktestSignalOut(BaseModel):
    trade_date: str
    selected: FactorEffectBacktestSelectedOut
    best_future_style: str | None = None
    best_future_return_pct: float | None = None


class FactorEffectBacktestSweepOut(BaseModel):
    top_n: int
    min_sample: int
    signal_days: int
    avg_forward_return_pct: float | None = None
    hit_rate_pct: float | None = None
    best_match_rate_pct: float | None = None


class FactorEffectBacktestOut(BaseModel):
    status: str
    label_source: str
    label_source_name: str
    method: str
    diagnosis: str
    mainline_text: str
    is_statistically_valid: bool
    lookback_days: int
    momentum_period: int
    forward_period: int
    top_n: int
    min_sample: int
    tested_days: int
    signal_days: int
    tagged_stock_count: int
    tag_link_count: int
    avg_forward_return_pct: float | None = None
    avg_forward_excess_pct: float | None = None
    hit_rate_pct: float | None = None
    best_match_rate_pct: float | None = None
    best_mainlines: list[FactorEffectBacktestMainlineOut] = Field(default_factory=list)
    recent_signals: list[FactorEffectBacktestSignalOut] = Field(default_factory=list)
    parameter_sweep: list[FactorEffectBacktestSweepOut] = Field(default_factory=list)


class FactorEffectDailyLogOut(BaseModel):
    id: int
    trade_date: date
    status: str
    source_status: str
    phase_key: str | None = None
    phase_label: str | None = None
    action: str | None = None
    summary: str | None = None
    signal: dict[str, Any] = Field(default_factory=dict)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    filtered: list[dict[str, Any]] = Field(default_factory=list)
    performance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class FactorEffectDailyLogsResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None = None
    logs: list[FactorEffectDailyLogOut] = Field(default_factory=list)
    message: str


class FactorTagStockOut(BaseModel):
    code: str
    name: str
    exchange: str
    full_code: str
    latest_price: float | None
    change_pct: float | None
    return_pct: float | None
    tags: list[FactorTagOut]
    is_untagged: bool = False
    tag_status: str
    watchlist_added: bool = False
    watchlist_enabled: bool = False
    watchlist_push_enabled: bool = False
    updated_at: datetime | None = None


class FactorTagStockDetailOut(BaseModel):
    tag: FactorTagOut
    period: int
    direction: str
    direction_label: str
    average_return_pct: float | None
    sample_count: int
    coverage_count: int
    sample_stocks: list[FactorTagStockOut]
    all_stocks: list[FactorTagStockOut]


class FactorRefreshRunOut(BaseModel):
    id: int
    status: str
    message: str | None
    fetched_count: int
    cached_count: int
    failed_count: int
    started_at: datetime
    completed_at: datetime | None


class FactorOverview(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    active_preset: FactorPresetOut
    presets: list[FactorPresetOut]
    tags: list[FactorTagOut]
    stock_rankings: list[FactorPeriodRankingOut]
    tag_rankings: list[FactorPeriodRankingOut]
    effect_style: FactorEffectStyleOut | None = None
    review_stocks: list[FactorReviewStockOut] = Field(default_factory=list)
    latest_run: FactorRefreshRunOut | None
    message: str


class StockAnalysisRecordCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    summary: str = ""
    body: str = Field(min_length=1)
    source_type: str = Field(default="manual_note", max_length=40)
    source_ref: str | None = Field(default=None, max_length=160)
    verification_status: str = Field(default="待验证", max_length=32)
    suggested_tags: list[str] = Field(default_factory=list)
    source_summary: str | None = None


class StockAnalysisRecordOut(BaseModel):
    id: int
    code: str
    name: str
    exchange: str
    full_code: str
    title: str
    summary: str
    body: str
    source_type: str
    source_ref: str | None
    verification_status: str
    suggested_tags: list[str] = Field(default_factory=list)
    source_summary: str | None
    generation_item_id: int | None
    created_at: datetime
    updated_at: datetime


class StockResearchGenerationItemUpdate(BaseModel):
    action_status: str | None = Field(default=None, max_length=40)
    processed: bool | None = None
    ignored: bool | None = None
    summary: str | None = None
    thesis: str | None = None
    body: str | None = None
    verification_status: str | None = Field(default=None, max_length=32)
    source_summary: str | None = None
    selected_tags: list[str] | None = None


class StockResearchApplyTagsRequest(BaseModel):
    tag_names: list[str] | None = None


class StockResearchQqItemCreate(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str | None = Field(default=None, max_length=80)
    summary: str = ""
    thesis: str = ""
    body: str = ""
    market_tags: list[str] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list)
    selected_tags: list[str] | None = None
    suggested_market_style: str | None = Field(default=None, max_length=120)
    verification_status: str = Field(default="待验证", max_length=32)
    source_summary: str | None = None
    status: str = Field(default="generated", max_length=40)
    error_message: str | None = None


class StockResearchQqItemsCreate(BaseModel):
    title: str | None = Field(default=None, max_length=240)
    trigger_type: str = Field(default="qq_chat", max_length=40)
    template_name: str = Field(default="qq", max_length=80)
    template_version: str | None = Field(default=None, max_length=40)
    source_ref: str | None = Field(default=None, max_length=160)
    message: str | None = None
    items: list[StockResearchQqItemCreate] = Field(min_length=1, max_length=200)


class StockResearchGenerationItemOut(BaseModel):
    id: int
    run_id: int
    code: str
    name: str
    exchange: str
    full_code: str
    status: str
    action_status: str
    summary: str
    thesis: str
    body: str
    market_tags: list[str] = Field(default_factory=list)
    suggested_tags: list[str] = Field(default_factory=list)
    selected_tags: list[str] = Field(default_factory=list)
    suggested_market_style: str | None
    verification_status: str
    source_summary: str | None
    analysis_record_id: int | None
    tags_applied: bool
    processed: bool
    ignored: bool
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class StockResearchGenerationRunOut(BaseModel):
    id: int
    title: str
    trigger_type: str
    template_name: str
    template_version: str
    source_ref: str
    status: str
    message: str | None
    item_count: int
    success_count: int
    failed_count: int
    applied_count: int
    processed_count: int
    ignored_count: int
    created_at: datetime
    completed_at: datetime | None
    updated_at: datetime


class StockResearchGenerationRunDetailOut(StockResearchGenerationRunOut):
    items: list[StockResearchGenerationItemOut] = Field(default_factory=list)


class StockResearchBatchInitialReviewOut(BaseModel):
    status: str
    message: str
    run: StockResearchGenerationRunDetailOut


class StockRelationOut(BaseModel):
    id: int | None = None
    name: str
    type: str
    detail: str | None = None
    link: str | None = None


class StockInformationFlowItemOut(BaseModel):
    id: str
    source_type: str
    source_name: str
    title: str
    content: str
    link: str | None = None
    created_at: datetime | None = None


class StockInformationFlowResponse(BaseModel):
    status: str
    items: list[StockInformationFlowItemOut] = Field(default_factory=list)
    ima_status: str = "not_configured"
    ima_message: str | None = None


class StockDetailOut(BaseModel):
    code: str
    name: str
    exchange: str
    full_code: str
    latest_price: float | None = None
    change_pct: float | None = None
    factor_tags: list[FactorTagOut] = Field(default_factory=list)
    factor_tag_status: str = "unreviewed"
    latest_generation_item: StockResearchGenerationItemOut | None = None
    analysis_records: list[StockAnalysisRecordOut] = Field(default_factory=list)
    relations: list[StockRelationOut] = Field(default_factory=list)
    information_flow: list[StockInformationFlowItemOut] = Field(default_factory=list)
    ima_status: str = "not_configured"
    ima_message: str | None = None


class MarketOpportunityGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    subtitle: str | None = Field(default=None, max_length=160)
    color: str | None = Field(default="#1d4ed8", max_length=24)
    sort_order: int = 100


class MarketOpportunityGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    subtitle: str | None = Field(default=None, max_length=160)
    color: str | None = Field(default=None, max_length=24)
    sort_order: int | None = None
    is_active: bool | None = None


class MarketOpportunityItemCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=120)
    stock_code: str | None = Field(default=None, max_length=24)
    feature_title: str | None = Field(default=None, max_length=160)
    feature_tags: list[str] = Field(default_factory=list)
    feature_desc: str | None = None
    order_checks: list[str] = Field(default_factory=list)
    replacement_space: str | None = None
    barriers: list[str] = Field(default_factory=list)
    highlight_level: int = Field(default=0, ge=0, le=2)
    verification_status: str = Field(min_length=1, max_length=24)
    source_note: str = Field(min_length=1)
    data_date: date
    sort_order: int = 100


class MarketOpportunityItemUpdate(BaseModel):
    group_id: int | None = None
    company_name: str | None = Field(default=None, min_length=1, max_length=120)
    stock_code: str | None = Field(default=None, max_length=24)
    feature_title: str | None = Field(default=None, max_length=160)
    feature_tags: list[str] | None = None
    feature_desc: str | None = None
    order_checks: list[str] | None = None
    replacement_space: str | None = None
    barriers: list[str] | None = None
    highlight_level: int | None = Field(default=None, ge=0, le=2)
    verification_status: str | None = Field(default=None, min_length=1, max_length=24)
    source_note: str | None = Field(default=None, min_length=1)
    data_date: date | None = None
    sort_order: int | None = None


class MarketOpportunityItemOut(BaseModel):
    id: int
    group_id: int
    company_name: str
    stock_code: str | None
    feature_title: str | None
    feature_tags: list[str] = Field(default_factory=list)
    feature_desc: str | None
    order_checks: list[str] = Field(default_factory=list)
    replacement_space: str | None
    barriers: list[str] = Field(default_factory=list)
    highlight_level: int
    verification_status: str
    source_note: str
    data_date: date
    sort_order: int
    created_at: datetime
    updated_at: datetime


class MarketOpportunityGroupOut(BaseModel):
    id: int
    name: str
    subtitle: str | None
    color: str | None
    sort_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime
    items: list[MarketOpportunityItemOut] = Field(default_factory=list)


class MarketOpportunityOverview(BaseModel):
    status: str
    updated_at: datetime | None
    message: str
    verification_statuses: list[str]
    groups: list[MarketOpportunityGroupOut]


class IndustryChainCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    summary: str | None = None
    phase: str = "观察"
    strength: int = Field(default=50, ge=0, le=100)
    catalyst: str | None = None
    risk: str | None = None
    status: str = "active"
    sort_order: int = 100


class IndustryChainUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    summary: str | None = None
    phase: str | None = None
    strength: int | None = Field(default=None, ge=0, le=100)
    catalyst: str | None = None
    risk: str | None = None
    status: str | None = None
    sort_order: int | None = None


class IndustryChainSegmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None
    sort_order: int = 100


class IndustryChainSegmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = None
    sort_order: int | None = None


class IndustryChainCompanyCreate(BaseModel):
    segment_id: int | None = None
    name: str = Field(min_length=1, max_length=120)
    stock_code: str | None = Field(default=None, max_length=24)
    full_code: str | None = Field(default=None, max_length=24)
    position: str | None = None
    elasticity_score: int = Field(default=50, ge=0, le=100)
    tracking_status: str = "观察"
    core_logic: str | None = None
    main_risk: str | None = None
    sort_order: int = 100


class IndustryChainCompanyUpdate(BaseModel):
    segment_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    stock_code: str | None = Field(default=None, max_length=24)
    full_code: str | None = Field(default=None, max_length=24)
    position: str | None = None
    elasticity_score: int | None = Field(default=None, ge=0, le=100)
    tracking_status: str | None = None
    core_logic: str | None = None
    main_risk: str | None = None
    sort_order: int | None = None


class IndustryChainEvidenceCreate(BaseModel):
    company_id: int | None = None
    title: str = Field(min_length=1, max_length=240)
    content: str | None = None
    source_name: str | None = Field(default=None, max_length=120)
    source_url: str | None = None
    impact_level: str = "中"
    evidence_date: date


class IndustryChainEvidenceUpdate(BaseModel):
    company_id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    content: str | None = None
    source_name: str | None = Field(default=None, max_length=120)
    source_url: str | None = None
    impact_level: str | None = None
    evidence_date: date | None = None


class IndustryChainTaskCreate(BaseModel):
    company_id: int | None = None
    title: str = Field(min_length=1, max_length=240)
    description: str | None = None
    priority: str = "中"
    status: str = "待验证"
    due_date: date | None = None
    conclusion: str | None = None


class IndustryChainTaskUpdate(BaseModel):
    company_id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    priority: str | None = None
    status: str | None = None
    due_date: date | None = None
    conclusion: str | None = None


class IndustryChainOpportunityLinkCreate(BaseModel):
    chain_id: int
    opportunity_item_id: int
    segment_id: int | None = None
    company_id: int | None = None


class IndustryChainOut(BaseModel):
    id: int
    name: str
    summary: str | None
    phase: str
    strength: int
    catalyst: str | None
    risk: str | None
    status: str
    sort_order: int
    created_at: datetime
    updated_at: datetime


class IndustryChainEvidenceOut(BaseModel):
    id: int
    chain_id: int
    company_id: int | None
    title: str
    content: str | None
    source_name: str | None
    source_url: str | None
    impact_level: str
    evidence_date: date
    created_at: datetime
    updated_at: datetime


class IndustryChainSummaryOut(IndustryChainOut):
    segment_count: int = 0
    company_count: int = 0
    open_task_count: int = 0
    opportunity_count: int = 0
    latest_evidence: IndustryChainEvidenceOut | None = None


class IndustryChainSegmentOut(BaseModel):
    id: int
    chain_id: int
    name: str
    description: str | None
    sort_order: int
    is_default: bool
    created_at: datetime
    updated_at: datetime


class IndustryChainCompanyOut(BaseModel):
    id: int
    chain_id: int
    segment_id: int | None
    code: str | None
    name: str
    exchange: str | None
    full_code: str | None
    position: str | None
    elasticity_score: int
    tracking_status: str
    core_logic: str | None
    main_risk: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime


class IndustryChainTaskOut(BaseModel):
    id: int
    chain_id: int
    company_id: int | None
    title: str
    description: str | None
    priority: str
    status: str
    due_date: date | None
    conclusion: str | None
    created_at: datetime
    updated_at: datetime


class IndustryChainAutoEvidenceOut(BaseModel):
    id: str
    source_type: str
    source_name: str
    title: str
    content: str | None
    link: str | None
    created_at: datetime | None
    company_name: str | None
    full_code: str | None


class IndustryChainOpportunityLinkOut(BaseModel):
    id: int
    chain_id: int
    segment_id: int | None
    company_id: int | None
    opportunity_item_id: int
    company_name: str
    stock_code: str | None
    feature_title: str | None
    feature_desc: str | None
    verification_status: str
    source_note: str
    updated_at: datetime


class IndustryChainDetailOut(IndustryChainOut):
    segments: list[IndustryChainSegmentOut] = Field(default_factory=list)
    companies: list[IndustryChainCompanyOut] = Field(default_factory=list)
    evidence: list[IndustryChainEvidenceOut] = Field(default_factory=list)
    auto_evidence: list[IndustryChainAutoEvidenceOut] = Field(default_factory=list)
    tasks: list[IndustryChainTaskOut] = Field(default_factory=list)
    opportunity_links: list[IndustryChainOpportunityLinkOut] = Field(default_factory=list)


class IndustryChainOverviewOut(BaseModel):
    status: str
    updated_at: datetime | None
    message: str
    phase_stats: dict[str, int] = Field(default_factory=dict)
    task_count: int = 0
    chains: list[IndustryChainSummaryOut] = Field(default_factory=list)


class SectorIndexCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    parent_name: str | None = Field(default=None, max_length=80)
    group_sort_order: int = 100
    source_image: str | None = None
    sort_order: int = 100


class SectorIndexUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    parent_name: str | None = Field(default=None, max_length=80)
    group_sort_order: int | None = None
    source_image: str | None = None
    sort_order: int | None = None
    active: bool | None = None


class SectorIndexSummaryOut(BaseModel):
    id: int
    name: str
    description: str | None
    parent_name: str | None = None
    group_sort_order: int = 100
    source_image: str | None = None
    pinyin: str = ""
    pinyin_initials: str = ""
    active: bool
    sort_order: int
    member_count: int
    rising_member_count: int = 0
    falling_member_count: int = 0
    quoted_member_count: int = 0
    latest_close: float | None
    latest_change_pct: float | None
    latest_trade_date: date | None
    updated_at: datetime


class SectorIndexGroupOut(BaseModel):
    name: str
    sort_order: int
    sector_count: int
    member_count: int
    sectors: list[SectorIndexSummaryOut]


class SectorIndexOverview(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    message: str
    sectors: list[SectorIndexSummaryOut]
    groups: list[SectorIndexGroupOut] = Field(default_factory=list)


class SectorIndexMemberCreate(BaseModel):
    query: str = Field(min_length=1, max_length=120)
    source: str = Field(default="manual", max_length=32)
    source_note: str | None = Field(default=None, max_length=240)


class SectorIndexMemberOut(BaseModel):
    id: int
    sector_id: int
    code: str
    name: str
    exchange: str
    full_code: str
    source: str
    source_note: str | None
    latest_price: float | None
    change_pct: float | None
    factor_tags: list[FactorTagOut] = Field(default_factory=list)
    factor_tag_status: str
    created_at: datetime
    updated_at: datetime


class SectorIndexBarOut(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    change_pct: float | None
    amount: float | None = None
    member_count: int
    created_at: datetime


class StockBarOut(BaseModel):
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    change_pct: float | None
    amount: float | None = None


class SectorIndexDetailOut(BaseModel):
    sector: SectorIndexSummaryOut
    members: list[SectorIndexMemberOut]
    recent_returns: list[SectorIndexBarOut]


class SectorIndexRecalculateResult(BaseModel):
    status: str
    message: str
    sector: SectorIndexSummaryOut
    bar_count: int


class SectorIndexImageImportCandidate(BaseModel):
    source_text: str
    code: str | None = None
    name: str | None = None
    exchange: str | None = None
    full_code: str | None = None
    matched: bool = False
    exists: bool = False
    message: str | None = None


class SectorIndexImageImportResponse(BaseModel):
    status: str
    message: str
    candidates: list[SectorIndexImageImportCandidate]


class MarketStyleCacheRunOut(BaseModel):
    id: int
    status: str
    message: str | None = None
    imported_count: int = 0
    failed_count: int = 0
    started_at: datetime
    completed_at: datetime | None = None


class MarketStyleCacheStatusOut(BaseModel):
    status: str
    bar_count: int = 0
    stock_count: int = 0
    min_trade_date: date | None = None
    max_trade_date: date | None = None
    updated_at: datetime | None = None
    latest_run: MarketStyleCacheRunOut | None = None


class MarketStyleCacheRefreshResult(BaseModel):
    status: str
    message: str | None = None
    run: MarketStyleCacheRunOut
    cache: MarketStyleCacheStatusOut


class MarketStyleDirectionOut(BaseModel):
    id: int | str
    name: str
    group_type: str | None = None
    member_count: int = 0
    rising_member_count: int = 0
    new_high_count: int = 0
    latest_change_pct: float | None = None
    effect_score: float = 0
    overheat_score: float = 0
    heat_status: str = "低温"
    status: str = "弱方向"
    components: dict[str, float] = Field(default_factory=dict)
    quality_score: float = 100
    quality_status: str = "可信"
    quality_reasons: list[str] = Field(default_factory=list)
    coverage_ratio: float = 1
    active_candidate_count: int = 0
    breadth_score: float = 0
    sentiment_score: float = 0
    leadership_score: float = 0
    risk_score: float = 0
    persistence_score: float = 0
    market_state: str = "等待数据"
    score_reason: str | None = None
    risk_flags: list[str] = Field(default_factory=list)


class MarketStyleCandidateOut(BaseModel):
    full_code: str
    code: str
    name: str
    trade_date: date
    latest_close: float | None = None
    change_pct: float | None = None
    strength_score: float = 0
    support_score: float = 0
    overheat_score: float = 0
    signal_score: float = 0
    heat_status: str = "低温"
    candidate_status: str = "可观察"
    return_20d: float = 0
    return_60d: float = 0
    amount_ratio: float = 1
    sector_id: int | None = None
    sector_name: str | None = None
    sector_type: str | None = None
    candidate_role: str = "噪音"
    role_reason: str | None = None


class MarketStyleGateOut(BaseModel):
    status: str = "等待数据"
    action: str = ""
    reason: str = ""
    top_direction_score: float = 0
    top3_avg_score: float = 0
    strong_direction_count: int = 0
    top_overheat_score: float = 0
    effect_score: float = 0
    breadth_score: float = 0
    sentiment_score: float = 0
    leadership_score: float = 0
    risk_score: float = 0
    persistence_score: float = 0
    market_state: str = "等待数据"
    score_reason: str | None = None
    risk_flags: list[str] = Field(default_factory=list)
    components: dict[str, float] = Field(default_factory=dict)


class MarketStyleScopeOut(BaseModel):
    name: str
    trade_date: date | None = None
    directions: list[MarketStyleDirectionOut] = Field(default_factory=list)
    candidates: list[MarketStyleCandidateOut] = Field(default_factory=list)
    gate: MarketStyleGateOut = Field(default_factory=MarketStyleGateOut)


class MarketStyleBacktestResult(BaseModel):
    status: str
    message: str
    initial_cash: float = 1_000_000
    ending_equity: float | None = None
    return_pct: float | None = None
    max_drawdown_pct: float | None = None
    trade_count: int | None = None
    trade_samples: list[dict[str, str | float | int | None]] = Field(default_factory=list)
    stdout: str = ""


class JudgmentMotherCardOut(BaseModel):
    id: str
    title: str
    problem: str
    call_conditions: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    supporting_card_ids: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    revision_notes: list[str] = Field(default_factory=list)
    reuse_hint: str


class JudgmentAssistantCardOut(BaseModel):
    id: str
    title: str
    core: str
    mother_card_ids: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    revision_notes: list[str] = Field(default_factory=list)
    reuse_hint: str


class JudgmentCardRelationOut(BaseModel):
    mother_card_id: str
    card_id: str


class JudgmentRecordCreate(BaseModel):
    judgment_object: str = Field(min_length=1, max_length=200)
    called_cards: list[str] = Field(default_factory=list)
    satisfied: str | None = None
    unsatisfied: str | None = None
    conclusion: str
    next_validation: str | None = None
    note: str | None = None


class JudgmentRecordOut(BaseModel):
    id: str
    title: str
    date: str
    judgment_object: str
    called_cards: list[str] = Field(default_factory=list)
    satisfied: str
    unsatisfied: str
    conclusion: str
    next_validation: str
    note: str


class JudgmentAssistantOverview(BaseModel):
    status: str
    message: str
    library_path: str
    updated_at: datetime | None
    mother_cards: list[JudgmentMotherCardOut] = Field(default_factory=list)
    cards: list[JudgmentAssistantCardOut]
    card_relations: list[JudgmentCardRelationOut] = Field(default_factory=list)
    recent_records: list[JudgmentRecordOut]


class WatchlistAnnouncementStockSearchOut(BaseModel):
    code: str
    name: str
    exchange: str
    full_code: str
    pinyin: str
    score: float


class WatchlistAnnouncementStockCreate(BaseModel):
    query: str = Field(min_length=1, max_length=80)


class WatchlistAnnouncementStockUpdate(BaseModel):
    enabled: bool | None = None
    pushEnabled: bool | None = None


class WatchlistAnnouncementStockOut(BaseModel):
    id: int
    code: str
    name: str
    exchange: str
    full_code: str
    enabled: bool
    pushEnabled: bool
    factor_tags: list[FactorTagOut] = Field(default_factory=list)
    factor_tag_status: str = "unreviewed"
    created_at: datetime
    updated_at: datetime


class WatchlistAnnouncementItemOut(BaseModel):
    id: int
    code: str
    name: str
    exchange: str
    full_code: str
    source_type: str
    source_name: str
    title: str
    summary: str
    source_url: str
    published_at: datetime | None
    crawled_at: datetime
    importance_level: str
    should_push: bool
    ai_status: str
    ai_reason: str | None
    push_status: str
    push_message: str | None
    pushed: bool


class WatchlistAnnouncementSourceOut(BaseModel):
    source_type: str
    source_name: str
    status: str
    message: str | None
    updated_at: datetime | None


class WatchlistAnnouncementCrawlLogOut(BaseModel):
    id: int
    source_type: str
    full_code: str | None
    status: str
    message: str | None
    fetched_count: int
    saved_count: int
    ignored_count: int
    created_at: datetime


class WatchlistAnnouncementPushLogOut(BaseModel):
    id: int
    item_id: int | None
    full_code: str
    group_name: str
    title: str
    body: str
    link: str | None
    status: str
    message: str | None
    created_at: datetime


class WatchlistAnnouncementsOverview(BaseModel):
    status: str
    source_status: str
    ai_status: str
    bark_status: str
    updated_at: datetime | None
    message: str
    stocks: list[WatchlistAnnouncementStockOut]
    items: list[WatchlistAnnouncementItemOut]
    sources: list[WatchlistAnnouncementSourceOut]
    crawl_logs: list[WatchlistAnnouncementCrawlLogOut]
    push_logs: list[WatchlistAnnouncementPushLogOut]


class WatchlistAnnouncementsCrawlResult(BaseModel):
    status: str
    message: str | None = None
    fetched_count: int = 0
    matched_count: int = 0
    ignored_count: int = 0


class WatchlistAnnouncementAiSettingsOut(BaseModel):
    provider: str
    model: str
    base_url: str
    enabled: bool
    api_key_configured: bool
    api_key_masked: str | None
    status: str
    effective_provider: str | None = None
    effective_model: str | None = None
    effective_base_url: str | None = None


class WatchlistAnnouncementAiSettingsUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    clear_api_key: bool = False


class CryptoWatchCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    leftExchange: str
    rightExchange: str
    leftMarketType: str = "futures"
    rightMarketType: str = "futures"
    enabled: bool = True
    note: str | None = None


class CryptoWatchUpdate(BaseModel):
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    leftExchange: str | None = None
    rightExchange: str | None = None
    leftMarketType: str | None = None
    rightMarketType: str | None = None
    enabled: bool | None = None
    note: str | None = None


class CryptoExchangeOptionOut(BaseModel):
    code: str
    name: str


class CryptoSymbolMappingCreate(BaseModel):
    inputSymbol: str = Field(min_length=1, max_length=32)
    exchange: str
    marketType: str = "futures"
    mappedSymbol: str = Field(min_length=1, max_length=64)
    priceRatio: float = Field(default=1.0, gt=0)
    note: str | None = None


class CryptoSymbolMappingUpdate(BaseModel):
    inputSymbol: str | None = Field(default=None, min_length=1, max_length=32)
    exchange: str | None = None
    marketType: str | None = None
    mappedSymbol: str | None = Field(default=None, min_length=1, max_length=64)
    priceRatio: float | None = Field(default=None, gt=0)
    note: str | None = None


class CryptoSymbolMappingOut(BaseModel):
    id: int
    inputSymbol: str
    exchange: str
    marketType: str
    mappedSymbol: str
    priceRatio: float
    note: str | None
    updatedAt: datetime


class CryptoSettingsUpdate(BaseModel):
    enabledExchanges: list[str]


class CryptoSettingsOut(BaseModel):
    availableExchanges: list[CryptoExchangeOptionOut]
    enabledExchanges: list[str]
    symbolMappings: list[CryptoSymbolMappingOut]


class CryptoWatchItemOut(BaseModel):
    id: int
    symbol: str
    leftExchange: str
    rightExchange: str
    leftMarketType: str
    rightMarketType: str
    label: str
    enabled: bool
    note: str | None
    leftBid: float | None
    leftAsk: float | None
    rightAsk: float | None
    rightBid: float | None
    spread: float | None
    spreadPct: float | None
    bidSpreadPct: float | None
    askSpreadPct: float | None
    leftFundingRate: float | None
    rightFundingRate: float | None
    leftPremiumRate: float | None
    rightPremiumRate: float | None
    leftMarkPrice: float | None
    rightMarkPrice: float | None
    leftIndexPrice: float | None
    rightIndexPrice: float | None
    status: str
    lastError: str | None
    updatedAt: datetime | None
    createdAt: datetime


class CryptoExchangeRowOut(BaseModel):
    exchange: str
    exchangeName: str
    symbol: str
    period: str
    fundingIntervalHours: float | None
    maxFundingRate: float | None
    minFundingRate: float | None
    currentFundingRate: float | None
    borrowStatus: str | None = None
    borrowMessage: str | None = None
    canBorrow: bool | None = None
    borrowableAmount: float | None = None
    borrowHourlyRate: float | None = None
    borrowDailyRate: float | None = None
    borrowPeriodRate: float | None = None
    fsNetFundingRate: float | None = None
    interestRate: float | None
    indexComponentRate: float | None
    premiumRate: float | None
    fundingRule: str | None
    fundingFormula: str | None
    premiumSource: str | None
    openInterest: float | None
    riskFund: float | None
    volume24h: float | None
    bestBid: float | None
    bestAsk: float | None
    markPrice: float | None
    indexPrice: float | None
    updatedAt: datetime | None
    status: str
    lastError: str | None


class CryptoMarginShortCheckOut(BaseModel):
    exchange: str
    symbol: str
    status: str
    message: str
    canBorrow: bool | None = None
    borrowableAmount: float | None = None
    hourlyBorrowRate: float | None = None
    dailyBorrowRate: float | None = None
    updatedAt: datetime | None = None


class CryptoPairSpreadOut(BaseModel):
    symbol: str
    leftExchange: str
    rightExchange: str
    leftMarketType: str
    rightMarketType: str
    pairType: str
    pair: str
    label: str
    spread: float | None
    spreadPct: float | None
    reverseSpreadPct: float | None
    bidSpreadPct: float | None
    askSpreadPct: float | None
    leftBid: float | None
    leftAsk: float | None
    rightBid: float | None
    rightAsk: float | None
    leftFundingRate: float | None = None
    rightFundingRate: float | None = None
    leftPremiumRate: float | None = None
    rightPremiumRate: float | None = None
    leftVolume24h: float | None = None
    rightVolume24h: float | None = None
    previousSpreadPct: float | None = None
    spreadJumpPct: float | None = None
    crossedWatch: bool = False
    crossedStrong: bool = False
    fundingDiffPct: float | None = None
    premiumDiffPct: float | None = None
    ffSignalLevel: str | None = None
    ffSignalReason: str | None = None
    status: str
    marginShortRequired: bool = False
    marginShortExchange: str | None = None
    marginShortSymbol: str | None = None
    marginShortStatus: str = "not_applicable"
    marginShortMessage: str | None = None
    marginShortCheck: CryptoMarginShortCheckOut | None = None


class CryptoAstroPairOut(CryptoPairSpreadOut):
    astroBidSpreadPct: float | None
    astroAskSpreadPct: float | None
    localBidSpreadPct: float | None
    localAskSpreadPct: float | None
    spreadDiffPct: float | None
    localMatched: bool
    source: str
    leftFundingRate: float | None
    rightFundingRate: float | None
    leftVolume24h: float | None
    rightVolume24h: float | None


class CryptoFundingHistoryItemOut(BaseModel):
    exchange: str
    symbol: str
    fundingRate: float | None
    premiumRate: float | None
    markPrice: float | None
    fundingTime: datetime | None


class CryptoFundingHistoryResponse(BaseModel):
    status: str
    source_status: str
    exchange: str
    exchangeName: str
    symbol: str
    items: list[CryptoFundingHistoryItemOut]
    updated_at: datetime | None
    message: str


class CryptoWatchHistoryPointOut(BaseModel):
    time: datetime
    spread: float | None
    spreadPct: float | None
    bidSpreadPct: float | None
    askSpreadPct: float | None
    leftBid: float | None
    leftAsk: float | None
    rightBid: float | None
    rightAsk: float | None
    leftFundingRate: float | None
    rightFundingRate: float | None
    leftPremiumRate: float | None
    rightPremiumRate: float | None
    leftMarkPrice: float | None
    rightMarkPrice: float | None
    leftIndexPrice: float | None
    rightIndexPrice: float | None
    status: str


class CryptoWatchHistoryResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    message: str
    item: CryptoWatchItemOut
    hours: int
    points: list[CryptoWatchHistoryPointOut]
    leftFundingHistory: list[CryptoFundingHistoryItemOut]
    rightFundingHistory: list[CryptoFundingHistoryItemOut]
    fundingErrors: list[str]


class CryptoOrderBookLevelOut(BaseModel):
    side: str
    level: int
    price: float | None
    size: float | None
    notional: float | None


class CryptoOrderBookOut(BaseModel):
    exchange: str
    exchangeName: str
    symbol: str
    marketType: str
    amountUnit: str
    turnover4hUsdt: float | None = None
    bids: list[CryptoOrderBookLevelOut]
    asks: list[CryptoOrderBookLevelOut]
    updatedAt: datetime | None
    status: str
    lastError: str | None


class CryptoPushRuleOut(BaseModel):
    id: int | None
    watchItemId: int
    enabled: bool
    openSpreadPct: float | None
    closeSpreadPct: float | None
    premiumDiffPct: float | None
    cooldownMinutes: int
    lastTriggeredAt: datetime | None
    updatedAt: datetime | None


class CryptoPushRuleUpdate(BaseModel):
    enabled: bool | None = None
    openSpreadPct: float | None = None
    closeSpreadPct: float | None = None
    premiumDiffPct: float | None = None
    cooldownMinutes: int | None = Field(default=None, ge=1, le=1440)


class CryptoPushLogOut(BaseModel):
    id: int
    watchItemId: int
    eventType: str
    title: str
    body: str
    spreadPct: float | None
    premiumDiffPct: float | None
    status: str
    message: str | None
    createdAt: datetime


class CryptoPushTestResponse(BaseModel):
    status: str
    message: str | None
    log: CryptoPushLogOut


class CryptoWatchOrderBooksResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    message: str
    item: CryptoWatchItemOut
    leftOrderBook: CryptoOrderBookOut
    rightOrderBook: CryptoOrderBookOut


class CryptoWatchDetailResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    message: str
    item: CryptoWatchItemOut
    leftOrderBook: CryptoOrderBookOut
    rightOrderBook: CryptoOrderBookOut
    history: CryptoWatchHistoryResponse
    pushRule: CryptoPushRuleOut
    pushLogs: list[CryptoPushLogOut]


class CryptoWatchlistResponse(BaseModel):
    status: str
    source_status: str
    updated_at: datetime | None
    items: list[CryptoWatchItemOut]
    message: str
    selectedSymbol: str
    supportedExchanges: list[str]
    supportedMarketTypes: dict[str, list[str]] = Field(default_factory=dict)
    minQuoteVolume24hUsdt: float = 0
    persistedQuoteCount: int = 0
    exchangeRows: list[CryptoExchangeRowOut]
    pairs: list[CryptoPairSpreadOut]
    boardStatus: str
    boardMessage: str
    boardUpdatedAt: datetime | None
    astroStatus: str
    astroMessage: str
    astroUpdatedAt: datetime | None
    astroPairs: list[CryptoAstroPairOut]


class CryptoRefreshResult(BaseModel):
    status: str
    message: str
    refreshed: int = 0
    failed: int = 0


class MarketReviewUniverseCreate(BaseModel):
    market: str
    symbol: str
    name: str
    theme: str = "AI叙事"
    role: str = "观察标的"
    enabled: bool = True
    sort_order: int = 100


class MarketReviewUniverseUpdate(BaseModel):
    market: str | None = None
    symbol: str | None = None
    name: str | None = None
    theme: str | None = None
    role: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None


class MarketReviewUniverseOut(BaseModel):
    id: int
    market: str
    symbol: str
    name: str
    theme: str
    role: str
    enabled: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class MarketReviewMaterialCreate(BaseModel):
    report_date: date | None = None
    source_name: str = "手动材料"
    market: str = "both"
    theme: str = "AI叙事"
    title: str
    content: str
    url: str | None = None
    importance: int = Field(default=3, ge=1, le=5)


class MarketReviewMaterialUpdate(BaseModel):
    report_date: date | None = None
    source_name: str | None = None
    market: str | None = None
    theme: str | None = None
    title: str | None = None
    content: str | None = None
    url: str | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    status: str | None = None


class MarketReviewMaterialOut(BaseModel):
    id: int
    report_date: str
    source_type: str
    source_name: str
    market: str
    theme: str
    title: str
    content: str
    url: str | None
    status: str
    importance: int
    created_at: datetime
    updated_at: datetime


class MarketReviewSourceStatus(BaseModel):
    source: str
    status: str
    message: str
    count: int = 0


class MarketReviewReportOut(BaseModel):
    id: int
    period: str
    report_date: str
    start_date: str
    end_date: str
    title: str
    status: str
    markdown_path: str
    content: str
    ai_status: str
    source_status: list[MarketReviewSourceStatus]
    created_at: datetime
    updated_at: datetime


class MarketReviewAiSettingsOut(BaseModel):
    provider: str
    model: str
    base_url: str
    enabled: bool
    api_key_configured: bool
    api_key_masked: str | None
    status: str
    effective_provider: str | None = None


class MarketReviewAiSettingsUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    clear_api_key: bool = False


class MarketReviewOverview(BaseModel):
    status: str
    source_status: str
    ai_status: str
    ai_settings: MarketReviewAiSettingsOut
    report_root: str
    message: str
    latest_report: MarketReviewReportOut | None
    reports: list[MarketReviewReportOut]
    materials: list[MarketReviewMaterialOut]
    universe: list[MarketReviewUniverseOut]


class MarketReviewDatePayload(BaseModel):
    report_date: date | None = None


class MarketReviewActionResult(BaseModel):
    status: str
    message: str | None = None
    report_date: str | None = None
    source_status: list[MarketReviewSourceStatus] = []
    report: MarketReviewReportOut | None = None
    matched_count: int = 0
    ignored_count: int = 0
