import { request } from "./apiClient";

export type SourceStatus =
  | "loading"
  | "ok"
  | "not_configured"
  | "manual_only"
  | "local_quotes_only"
  | "needs_authorization"
  | "partial_error"
  | "error"
  | "not_found"
  | "test"
  | "pending"
  | "pending_ai"
  | "running"
  | "skipped";

export interface SimplePageResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  items: unknown[];
  message: string;
}

export interface FactorTag {
  id: number;
  name: string;
  color: string | null;
  stock_count: number;
  created_at: string;
  updated_at: string;
  is_generic_direction?: boolean;
  trade_role?: "direction_only" | "candidate_mainline" | "confirmed_mainline" | "risk_only" | string;
  tag_kind?: string;
}

export interface FactorPreset {
  id: number;
  name: string;
  periods: number[];
  active: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface FactorTagPayload {
  name?: string;
  color?: string | null;
}

export interface FactorTagBulkPayload {
  names: string[];
}

export interface FactorPresetPayload {
  name?: string;
  periods?: number[];
  active?: boolean;
  sort_order?: number;
}

export interface FactorStockTagResponse {
  code: string;
  name: string;
  exchange: string;
  full_code: string;
  tags: FactorTag[];
  tag_status: FactorTagStatus;
  watchlist_added: boolean;
  watchlist_enabled: boolean;
  watchlist_push_enabled: boolean;
}

export type FactorTagStatus = "unreviewed" | "needs_more" | "done" | "excluded";

export interface FactorTagCandidateRun {
  id: number;
  status: SourceStatus;
  message: string | null;
  lookback_days: number;
  item_count: number;
  failed_count: number;
  started_at: string;
  completed_at: string | null;
}

export interface FactorTagCandidate {
  name: string;
  category: string;
  direction: string;
  score: number;
  reason: string;
  sources: string[];
  example_stocks: string[];
  returns: Record<string, number | string>;
}

export interface FactorTagCandidatesResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  candidates: FactorTagCandidate[];
  sources: Array<Record<string, unknown>>;
  latest_run: FactorTagCandidateRun | null;
  message: string;
}

export interface FactorTagCandidateRunPayload {
  lookback_days?: number;
  limit?: number;
}

export interface FactorRankingStock {
  code: string;
  name: string;
  exchange: string;
  full_code: string;
  latest_price: number | null;
  change_pct: number | null;
  return_pct: number;
  tags: FactorTag[];
  is_untagged: boolean;
  tag_status: FactorTagStatus;
  watchlist_added: boolean;
  watchlist_enabled: boolean;
  watchlist_push_enabled: boolean;
  updated_at: string;
}

export interface FactorReviewStock extends FactorRankingStock {
  reasons: string[];
  review_reasons: string[];
}

export interface FactorHotTagReviewStock extends FactorRankingStock {
  reasons: string[];
  suggested_tags: string[];
  amount: number | null;
}

export interface FactorHotTagStatStock {
  full_code: string;
  code: string;
  name: string;
  change_pct: number | null;
  return_pct: number | null;
  amount: number | null;
  sources: string[];
}

export interface FactorHotTagStat {
  tag_id: number;
  tag_name: string;
  total_count: number;
  gain_count: number;
  amount_count: number;
  overlap_count: number;
  sample_stocks: FactorHotTagStatStock[];
}

export interface FactorHotTagReviewResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  message: string;
  gain_limit: number;
  amount_limit: number;
  sample_count: number;
  tagged_sample_count: number;
  untagged_sample_count: number;
  tag_stats: FactorHotTagStat[];
  stocks: FactorHotTagReviewStock[];
}

export interface FactorAutoTagRun {
  id: number;
  status: SourceStatus;
  message: string | null;
  stock_count: number;
  tagged_count: number;
  tag_count: number;
  source_statuses: Array<Record<string, unknown>>;
  started_at: string;
  completed_at: string | null;
}

export interface FactorAutoTagResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  latest_run: FactorAutoTagRun | null;
  ima_cache: Record<string, unknown>;
  message: string;
}

export interface FactorTaggedStocksResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  message: string;
  total_count: number;
  tagged_count: number;
  stocks: FactorHotTagReviewStock[];
}

export interface FactorTagRanking {
  tag_id: number | null;
  tag_name: string;
  color: string | null;
  stock_count: number;
  average_return_pct: number;
  stock_codes: string[];
  system: boolean;
  evidence_days?: number;
  evidence_level?: string;
  is_generic_direction?: boolean;
  trade_role?: "direction_only" | "candidate_mainline" | "confirmed_mainline" | "risk_only" | string;
}

export interface FactorPeriodRanking {
  period: number;
  gainers: FactorRankingStock[];
  losers: FactorRankingStock[];
  winning_tags: FactorTagRanking[];
  losing_tags: FactorTagRanking[];
  board_winning_tags: FactorTagRanking[];
  board_losing_tags: FactorTagRanking[];
  coverage_count: number;
}

export interface FactorTagStock extends Omit<FactorRankingStock, "return_pct" | "updated_at"> {
  return_pct: number | null;
  updated_at: string | null;
}

export interface FactorTagStockDetail {
  tag: FactorTag;
  period: number;
  direction: "winning" | "losing";
  direction_label: string;
  average_return_pct: number | null;
  sample_count: number;
  coverage_count: number;
  sample_stocks: FactorTagStock[];
  all_stocks: FactorTagStock[];
}

export interface FactorRefreshRun {
  id: number;
  status: SourceStatus;
  message: string | null;
  fetched_count: number;
  cached_count: number;
  failed_count: number;
  started_at: string;
  completed_at: string | null;
}

export interface FactorEffectStyleTag extends FactorTagRanking {
  periods: number[];
}

export interface FactorRecentStyleTag {
  name: string;
  category: string;
  score: number;
  description: string;
  action: string;
  source_tags: string[];
}

export interface FactorEffectStyle {
  rules_enabled: boolean;
  style_name: string;
  summary: string;
  confidence: number;
  health: string;
  coverage_rate: number;
  coverage_label: string;
  coverage_count: number;
  tagged_count: number;
  total_count: number;
  primary_style: FactorRecentStyleTag | null;
  recent_style_tags: FactorRecentStyleTag[];
  participation_advice: string | null;
  avoid_advice: string | null;
  dominant_tags: FactorEffectStyleTag[];
  risk_tags: FactorEffectStyleTag[];
  board_dominant_tags: FactorEffectStyleTag[];
  board_risk_tags: FactorEffectStyleTag[];
  tradable_mainlines: FactorEffectStyleTag[];
  confirmed_mainlines: FactorEffectStyleTag[];
  direction_only_tags: FactorEffectStyleTag[];
  downgraded_tags: FactorEffectStyleTag[];
  current_phase: {
    key?: string;
    label?: string;
    description?: string;
  };
  risk_flags: string[];
  reasons: string[];
  action: string | null;
  market: {
    trade_date?: string | null;
    base_effect_score?: number | null;
    breadth_score?: number | null;
    sentiment_score?: number | null;
    risk_score?: number | null;
    persistence_score?: number | null;
    effect_score_delta_5d?: number | null;
    rising_ratio?: number | null;
    components?: Record<string, number | string | null>;
  };
}

export interface FactorEffectBacktestMainline {
  style_name: string;
  category: string;
  days_selected: number;
  avg_forward_return_pct: number | null;
  avg_forward_excess_pct: number | null;
  win_rate_pct: number | null;
  avg_momentum_return_pct: number | null;
  avg_sample_count: number | null;
  source_tags: string[];
  latest_seen: string | null;
  evidence_days?: number;
  evidence_level?: string;
  is_generic_direction?: boolean;
  trade_role?: string;
}

export interface FactorEffectBacktestSelected {
  style_name: string;
  category: string;
  score: number;
  sample_count: number;
  tag_count: number;
  source_tags: string[];
  avg_momentum_return_pct: number | null;
  avg_forward_return_pct: number | null;
  avg_forward_excess_pct: number | null;
  evidence_days?: number;
  evidence_level?: string;
  is_generic_direction?: boolean;
  trade_role?: string;
}

export interface FactorEffectBacktestSignal {
  trade_date: string;
  selected: FactorEffectBacktestSelected;
  best_future_style: string | null;
  best_future_return_pct: number | null;
}

export interface FactorEffectBacktestSweep {
  top_n: number;
  min_sample: number;
  signal_days: number;
  avg_forward_return_pct: number | null;
  hit_rate_pct: number | null;
  best_match_rate_pct: number | null;
}

export interface FactorEffectBacktest {
  status: string;
  label_source: string;
  label_source_name: string;
  method: string;
  diagnosis: string;
  mainline_text: string;
  is_statistically_valid: boolean;
  lookback_days: number;
  momentum_period: number;
  forward_period: number;
  top_n: number;
  min_sample: number;
  tested_days: number;
  signal_days: number;
  tagged_stock_count: number;
  tag_link_count: number;
  avg_forward_return_pct: number | null;
  avg_forward_excess_pct: number | null;
  hit_rate_pct: number | null;
  best_match_rate_pct: number | null;
  best_mainlines: FactorEffectBacktestMainline[];
  recent_signals: FactorEffectBacktestSignal[];
  parameter_sweep: FactorEffectBacktestSweep[];
}

export interface FactorEffectDailyLog {
  id: number;
  trade_date: string;
  status: SourceStatus;
  source_status: SourceStatus;
  phase_key: string | null;
  phase_label: string | null;
  action: string | null;
  summary: string | null;
  signal: Record<string, unknown>;
  candidates: Array<Record<string, unknown>>;
  filtered: Array<Record<string, unknown>>;
  performance: {
    status?: string;
    horizons?: Record<string, { avg_return_pct: number | null; win_rate_pct: number | null; sample_count: number }>;
    items?: Array<Record<string, unknown>>;
    updated_at?: string;
  };
  created_at: string;
  updated_at: string;
}

export interface FactorEffectDailyLogsResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  logs: FactorEffectDailyLog[];
  message: string;
}

export interface FactorOverview {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  active_preset: FactorPreset | null;
  presets: FactorPreset[];
  tags: FactorTag[];
  stock_rankings: FactorPeriodRanking[];
  tag_rankings: FactorPeriodRanking[];
  effect_style: FactorEffectStyle | null;
  review_stocks: FactorReviewStock[];
  latest_run: FactorRefreshRun | null;
  message: string;
}

export interface FactorStatusResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  message?: string | null;
}

export interface StockAnalysisRecord {
  id: number;
  code: string;
  name: string;
  exchange: string;
  full_code: string;
  title: string;
  summary: string;
  body: string;
  source_type: string;
  source_ref: string | null;
  verification_status: string;
  suggested_tags: string[];
  source_summary: string | null;
  generation_item_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface StockAnalysisRecordPayload {
  title: string;
  summary?: string;
  body: string;
  source_type?: string;
  source_ref?: string | null;
  verification_status?: string;
  suggested_tags?: string[];
  source_summary?: string | null;
}

export interface StockResearchGenerationItem {
  id: number;
  run_id: number;
  code: string;
  name: string;
  exchange: string;
  full_code: string;
  status: string;
  action_status: string;
  summary: string;
  thesis: string;
  body: string;
  market_tags: string[];
  suggested_tags: string[];
  selected_tags: string[];
  suggested_market_style: string | null;
  verification_status: string;
  source_summary: string | null;
  analysis_record_id: number | null;
  tags_applied: boolean;
  processed: boolean;
  ignored: boolean;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface StockResearchGenerationRun {
  id: number;
  title: string;
  trigger_type: string;
  template_name: string;
  template_version: string;
  source_ref: string;
  status: string;
  message: string | null;
  item_count: number;
  success_count: number;
  failed_count: number;
  applied_count: number;
  processed_count: number;
  ignored_count: number;
  created_at: string;
  completed_at: string | null;
  updated_at: string;
}

export interface StockResearchGenerationRunDetail extends StockResearchGenerationRun {
  items: StockResearchGenerationItem[];
}

export interface StockResearchActionResult {
  status: string;
  message: string;
  run: StockResearchGenerationRunDetail;
}

export interface StockResearchQqItemPayload {
  code: string;
  name?: string | null;
  summary?: string;
  thesis?: string;
  body?: string;
  market_tags?: string[];
  suggested_tags?: string[];
  selected_tags?: string[] | null;
  suggested_market_style?: string | null;
  verification_status?: string;
  source_summary?: string | null;
  status?: string;
  error_message?: string | null;
}

export interface StockResearchQqItemsPayload {
  title?: string | null;
  trigger_type?: string;
  template_name?: "qq" | "majia" | "keke" | "qq_majia" | string;
  template_version?: string;
  source_ref?: string | null;
  message?: string | null;
  items: StockResearchQqItemPayload[];
}

export interface StockResearchGenerationItemUpdatePayload {
  action_status?: string;
  processed?: boolean;
  ignored?: boolean;
  summary?: string;
  thesis?: string;
  body?: string;
  verification_status?: string;
  source_summary?: string | null;
  selected_tags?: string[];
}

export interface StockRelation {
  id: number | null;
  name: string;
  type: string;
  detail: string | null;
  link: string | null;
}

export interface StockInformationFlowItem {
  id: string;
  source_type: string;
  source_name: string;
  title: string;
  content: string;
  link: string | null;
  created_at: string | null;
}

export interface StockDetail {
  code: string;
  name: string;
  exchange: string;
  full_code: string;
  latest_price: number | null;
  change_pct: number | null;
  factor_tags: FactorTag[];
  factor_tag_status: FactorTagStatus;
  latest_generation_item: StockResearchGenerationItem | null;
  analysis_records: StockAnalysisRecord[];
  relations: StockRelation[];
  information_flow: StockInformationFlowItem[];
  ima_status: SourceStatus;
  ima_message: string | null;
}

export interface StockInformationFlowResponse {
  status: SourceStatus;
  items: StockInformationFlowItem[];
  ima_status: SourceStatus;
  ima_message: string | null;
}

export interface SectorIndexSummary {
  id: number;
  name: string;
  description: string | null;
  parent_name?: string | null;
  group_sort_order?: number;
  source_image?: string | null;
  pinyin: string;
  pinyin_initials: string;
  active: boolean;
  sort_order: number;
  member_count: number;
  rising_member_count: number;
  falling_member_count: number;
  quoted_member_count: number;
  latest_close: number | null;
  latest_change_pct: number | null;
  latest_trade_date: string | null;
  updated_at: string;
}

export interface SectorIndexGroup {
  name: string;
  sort_order: number;
  sector_count: number;
  member_count: number;
  sectors: SectorIndexSummary[];
}

export interface SectorIndexOverview {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  message: string;
  sectors: SectorIndexSummary[];
  groups?: SectorIndexGroup[];
}

export interface SectorIndexPayload {
  name?: string;
  description?: string | null;
  parent_name?: string | null;
  group_sort_order?: number;
  source_image?: string | null;
  sort_order?: number;
  active?: boolean;
}

export interface SectorIndexMember {
  id: number;
  sector_id: number;
  code: string;
  name: string;
  exchange: string;
  full_code: string;
  source: string;
  source_note: string | null;
  latest_price: number | null;
  change_pct: number | null;
  factor_tags: FactorTag[];
  factor_tag_status: FactorTagStatus;
  created_at: string;
  updated_at: string;
}

export interface SectorIndexBar {
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  change_pct: number | null;
  amount?: number | null;
  member_count: number;
  created_at: string;
}

export interface MarketStyleCacheRun {
  id: number;
  status: string;
  message: string | null;
  imported_count: number;
  failed_count: number;
  started_at: string;
  completed_at: string | null;
}

export interface MarketStyleCacheStatus {
  status: string;
  bar_count: number;
  stock_count: number;
  min_trade_date: string | null;
  max_trade_date: string | null;
  updated_at: string | null;
  latest_run: MarketStyleCacheRun | null;
}

export interface MarketStyleDirection {
  id: number | string;
  name: string;
  group_type?: string | null;
  member_count: number;
  rising_member_count: number;
  new_high_count: number;
  latest_change_pct?: number | null;
  effect_score: number;
  overheat_score: number;
  heat_status: string;
  status: string;
  components: Record<string, number>;
  quality_score: number;
  quality_status: string;
  quality_reasons: string[];
  coverage_ratio: number;
  active_candidate_count: number;
  breadth_score: number;
  sentiment_score: number;
  leadership_score: number;
  risk_score: number;
  persistence_score: number;
  market_state: string;
  score_reason?: string | null;
  risk_flags: string[];
}

export interface MarketStyleCandidate {
  full_code: string;
  code: string;
  name: string;
  trade_date: string;
  latest_close: number | null;
  change_pct: number | null;
  strength_score: number;
  support_score: number;
  overheat_score: number;
  signal_score: number;
  heat_status: string;
  candidate_status: string;
  return_20d: number;
  return_60d: number;
  amount_ratio: number;
  sector_id?: number | null;
  sector_name?: string | null;
  sector_type?: string | null;
  candidate_role: string;
  role_reason?: string | null;
}

export interface MarketStyleGate {
  status: string;
  action: string;
  reason: string;
  top_direction_score: number;
  top3_avg_score: number;
  strong_direction_count: number;
  top_overheat_score: number;
  effect_score: number;
  breadth_score: number;
  sentiment_score: number;
  leadership_score: number;
  risk_score: number;
  persistence_score: number;
  market_state: string;
  score_reason?: string | null;
  risk_flags: string[];
  components: Record<string, number>;
}

export interface MarketStyleScope {
  name: string;
  trade_date?: string | null;
  directions: MarketStyleDirection[];
  candidates: MarketStyleCandidate[];
  gate: MarketStyleGate;
}

export interface MarketStyleCacheRefreshResult {
  status: string;
  message: string | null;
  run: MarketStyleCacheRun;
  cache: MarketStyleCacheStatus;
}

export interface MarketStyleBacktestResult {
  status: string;
  message: string;
  initial_cash: number;
  ending_equity: number | null;
  return_pct: number | null;
  max_drawdown_pct: number | null;
  trade_count: number | null;
  trade_samples: Array<Record<string, string | number | null>>;
  stdout: string;
}

export interface StockBar {
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  change_pct: number | null;
  amount?: number | null;
}

export type OpportunityVerificationStatus = "待验证" | "部分验证" | "已验证" | "证伪";

export interface OpportunityItem {
  id: number;
  group_id: number;
  company_name: string;
  stock_code: string | null;
  feature_title: string | null;
  feature_tags: string[];
  feature_desc: string | null;
  order_checks: string[];
  replacement_space: string | null;
  barriers: string[];
  highlight_level: number;
  verification_status: OpportunityVerificationStatus;
  source_note: string;
  data_date: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface OpportunityGroup {
  id: number;
  name: string;
  subtitle: string | null;
  color: string | null;
  sort_order: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  items: OpportunityItem[];
}

export interface OpportunityOverview {
  status: string;
  message: string;
  updated_at: string | null;
  verification_statuses: OpportunityVerificationStatus[];
  groups: OpportunityGroup[];
}

export interface OpportunityGroupPayload {
  name?: string;
  subtitle?: string | null;
  color?: string | null;
  sort_order?: number;
  is_active?: boolean;
}

export interface OpportunityItemPayload {
  group_id?: number;
  company_name?: string;
  stock_code?: string | null;
  feature_title?: string | null;
  feature_tags?: string[];
  feature_desc?: string | null;
  order_checks?: string[];
  replacement_space?: string | null;
  barriers?: string[];
  highlight_level?: number;
  verification_status?: OpportunityVerificationStatus;
  source_note?: string;
  data_date?: string;
  sort_order?: number;
}

export interface IndustryChain {
  id: number;
  name: string;
  summary: string | null;
  phase: string;
  strength: number;
  catalyst: string | null;
  risk: string | null;
  status: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface IndustryChainEvidence {
  id: number;
  chain_id: number;
  company_id: number | null;
  title: string;
  content: string | null;
  source_name: string | null;
  source_url: string | null;
  impact_level: string;
  evidence_date: string;
  created_at: string;
  updated_at: string;
}

export interface IndustryChainSummary extends IndustryChain {
  segment_count: number;
  company_count: number;
  open_task_count: number;
  opportunity_count: number;
  latest_evidence: IndustryChainEvidence | null;
}

export interface IndustryChainSegment {
  id: number;
  chain_id: number;
  name: string;
  description: string | null;
  sort_order: number;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface IndustryChainCompany {
  id: number;
  chain_id: number;
  segment_id: number | null;
  code: string | null;
  name: string;
  exchange: string | null;
  full_code: string | null;
  position: string | null;
  elasticity_score: number;
  tracking_status: string;
  core_logic: string | null;
  main_risk: string | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface IndustryChainTask {
  id: number;
  chain_id: number;
  company_id: number | null;
  title: string;
  description: string | null;
  priority: string;
  status: string;
  due_date: string | null;
  conclusion: string | null;
  created_at: string;
  updated_at: string;
}

export interface IndustryChainAutoEvidence {
  id: string;
  source_type: string;
  source_name: string;
  title: string;
  content: string | null;
  link: string | null;
  created_at: string | null;
  company_name: string | null;
  full_code: string | null;
}

export interface IndustryChainOpportunityLink {
  id: number;
  chain_id: number;
  segment_id: number | null;
  company_id: number | null;
  opportunity_item_id: number;
  company_name: string;
  stock_code: string | null;
  feature_title: string | null;
  feature_desc: string | null;
  verification_status: string;
  source_note: string;
  updated_at: string;
}

export interface IndustryChainDetail extends IndustryChain {
  segments: IndustryChainSegment[];
  companies: IndustryChainCompany[];
  evidence: IndustryChainEvidence[];
  auto_evidence: IndustryChainAutoEvidence[];
  tasks: IndustryChainTask[];
  opportunity_links: IndustryChainOpportunityLink[];
}

export interface IndustryChainOverview {
  status: string;
  updated_at: string | null;
  message: string;
  phase_stats: Record<string, number>;
  task_count: number;
  chains: IndustryChainSummary[];
}

export interface IndustryChainPayload {
  name?: string;
  summary?: string | null;
  phase?: string;
  strength?: number;
  catalyst?: string | null;
  risk?: string | null;
  status?: string;
  sort_order?: number;
}

export interface IndustryChainSegmentPayload {
  name?: string;
  description?: string | null;
  sort_order?: number;
}

export interface IndustryChainCompanyPayload {
  segment_id?: number | null;
  name?: string;
  stock_code?: string | null;
  full_code?: string | null;
  position?: string | null;
  elasticity_score?: number;
  tracking_status?: string;
  core_logic?: string | null;
  main_risk?: string | null;
  sort_order?: number;
}

export interface IndustryChainEvidencePayload {
  company_id?: number | null;
  title?: string;
  content?: string | null;
  source_name?: string | null;
  source_url?: string | null;
  impact_level?: string;
  evidence_date?: string;
}

export interface IndustryChainTaskPayload {
  company_id?: number | null;
  title?: string;
  description?: string | null;
  priority?: string;
  status?: string;
  due_date?: string | null;
  conclusion?: string | null;
}

export interface IndustryChainOpportunityLinkPayload {
  chain_id: number;
  opportunity_item_id: number;
  segment_id?: number | null;
  company_id?: number | null;
}

export interface SectorIndexDetail {
  sector: SectorIndexSummary;
  members: SectorIndexMember[];
  recent_returns: SectorIndexBar[];
}

export interface SectorIndexRecalculateResult {
  status: SourceStatus;
  message: string;
  sector: SectorIndexSummary;
  bar_count: number;
}

export interface SectorIndexImageCandidate {
  source_text: string;
  code: string | null;
  name: string | null;
  exchange: string | null;
  full_code: string | null;
  matched: boolean;
  exists: boolean;
  message: string | null;
}

export interface SectorIndexImageImportResponse {
  status: SourceStatus;
  message: string;
  candidates: SectorIndexImageCandidate[];
}

export type CryptoExchange = "bn" | "by" | "gt" | "okx" | "bg" | "htx" | "as" | "hl";
export type CryptoMarketType = "spot" | "futures";

export interface CryptoExchangeOption {
  code: CryptoExchange;
  name: string;
}

export interface CryptoSymbolMapping {
  id: number;
  inputSymbol: string;
  exchange: CryptoExchange;
  marketType: CryptoMarketType;
  mappedSymbol: string;
  priceRatio: number;
  note: string | null;
  updatedAt: string;
}

export interface CryptoSettings {
  availableExchanges: CryptoExchangeOption[];
  enabledExchanges: CryptoExchange[];
  symbolMappings: CryptoSymbolMapping[];
}

export interface CryptoSettingsPayload {
  enabledExchanges: CryptoExchange[];
}

export interface CryptoSymbolMappingPayload {
  inputSymbol: string;
  exchange: CryptoExchange;
  marketType: CryptoMarketType;
  mappedSymbol: string;
  priceRatio?: number;
  note?: string | null;
}

export interface CryptoSymbolMappingUpdatePayload {
  inputSymbol?: string;
  exchange?: CryptoExchange;
  marketType?: CryptoMarketType;
  mappedSymbol?: string;
  priceRatio?: number;
  note?: string | null;
}

export interface CryptoSymbolMappingScanItem {
  inputSymbol: string;
  exchange: CryptoExchange;
  marketType: CryptoMarketType;
  mappedSymbol: string;
  priceRatio: number;
  note: string | null;
  action: "created" | "updated" | "unchanged";
}

export interface CryptoSymbolMappingScanResponse {
  status: string;
  startedAt: string;
  finishedAt: string;
  dryRun: boolean;
  exchangeCount: number;
  marketCount: number;
  symbolCount: number;
  candidateCount: number;
  faceValueCandidateCount: number;
  aliasCandidateCount: number;
  aliasScannedCount: number;
  aliasScanErrorCount: number;
  createdCount: number;
  updatedCount: number;
  unchangedCount: number;
  skippedCount: number;
  errorCount: number;
  items: CryptoSymbolMappingScanItem[];
  skipped: Array<Record<string, unknown>>;
  errors: Array<{ exchange: string; marketType: string; message: string }>;
  aliasScanErrors: Array<{ exchange: string; symbol: string; message: string }>;
}

export interface CryptoPairHistoryParams {
  symbol: string;
  leftExchange: CryptoExchange;
  rightExchange: CryptoExchange;
  leftMarketType: CryptoMarketType;
  rightMarketType: CryptoMarketType;
}

export interface CryptoWatchItem {
  id: number;
  symbol: string;
  leftExchange: CryptoExchange;
  rightExchange: CryptoExchange;
  leftMarketType: CryptoMarketType;
  rightMarketType: CryptoMarketType;
  label: string;
  enabled: boolean;
  note: string | null;
  leftBid: number | null;
  leftAsk: number | null;
  rightBid: number | null;
  rightAsk: number | null;
  spread: number | null;
  spreadPct: number | null;
  bidSpreadPct: number | null;
  askSpreadPct: number | null;
  leftFundingRate: number | null;
  rightFundingRate: number | null;
  leftPremiumRate: number | null;
  rightPremiumRate: number | null;
  leftMarkPrice: number | null;
  rightMarkPrice: number | null;
  leftIndexPrice: number | null;
  rightIndexPrice: number | null;
  status: SourceStatus;
  lastError: string | null;
  updatedAt: string | null;
  createdAt: string;
}

export interface CryptoWatchlistResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  items: CryptoWatchItem[];
  message: string;
  selectedSymbol: string;
  supportedExchanges: string[];
  supportedMarketTypes: Partial<Record<CryptoExchange, CryptoMarketType[]>>;
  minQuoteVolume24hUsdt: number;
  persistedQuoteCount: number;
  exchangeRows: CryptoExchangeRow[];
  pairs: CryptoPairSpread[];
  boardStatus: SourceStatus;
  boardMessage: string;
  boardUpdatedAt: string | null;
  astroStatus: SourceStatus;
  astroMessage: string;
  astroUpdatedAt: string | null;
  astroPairs: CryptoAstroPair[];
}

export interface CryptoExchangeRow {
  exchange: CryptoExchange;
  exchangeName: string;
  symbol: string;
  period: string;
  fundingIntervalHours: number | null;
  maxFundingRate: number | null;
  minFundingRate: number | null;
  currentFundingRate: number | null;
  borrowStatus: SourceStatus | "not_applicable" | "not_supported" | "not_borrowable" | "pending" | null;
  borrowMessage: string | null;
  canBorrow: boolean | null;
  borrowableAmount: number | null;
  borrowableValueUsdt: number | null;
  borrowHourlyRate: number | null;
  borrowDailyRate: number | null;
  borrowPeriodRate: number | null;
  fsNetFundingRate: number | null;
  interestRate: number | null;
  indexComponentRate: number | null;
  premiumRate: number | null;
  fundingRule: string | null;
  fundingFormula: string | null;
  premiumSource: string | null;
  openInterest: number | null;
  riskFund: number | null;
  volume24h: number | null;
  bestBid: number | null;
  bestAsk: number | null;
  markPrice: number | null;
  indexPrice: number | null;
  updatedAt: string | null;
  status: SourceStatus;
  lastError: string | null;
}

export interface CryptoMarginShortCheck {
  exchange: CryptoExchange;
  symbol: string;
  status: SourceStatus | "not_applicable" | "not_supported" | "not_borrowable" | "pending";
  message: string;
  canBorrow: boolean | null;
  borrowableAmount: number | null;
  borrowableValueUsdt: number | null;
  hourlyBorrowRate: number | null;
  dailyBorrowRate: number | null;
  updatedAt: string | null;
}

export interface CryptoCoinChainStatus {
  chain: string;
  depositEnabled: boolean | null;
  withdrawEnabled: boolean | null;
  withdrawFee: number | null;
  minWithdraw: number | null;
}

export interface CryptoCoinTransferStatus {
  exchange: CryptoExchange;
  symbol: string;
  status: SourceStatus | "not_supported" | "pending";
  message: string;
  depositEnabled: boolean | null;
  withdrawEnabled: boolean | null;
  chains: CryptoCoinChainStatus[];
  updatedAt: string | null;
}

export interface CryptoCoinIndexComponent {
  component: string;
  weight: number | null;
  rawWeight: string | null;
  source: string | null;
  price: number | null;
}

export interface CryptoCoinIndexStatus {
  exchange: CryptoExchange;
  symbol: string;
  status: SourceStatus | "not_supported" | "pending";
  message: string;
  components: CryptoCoinIndexComponent[];
  updatedAt: string | null;
}

export interface CryptoCoinExchangeStatus {
  exchange: CryptoExchange;
  exchangeName: string;
  symbol: string;
  status: SourceStatus | "pending";
  message: string | null;
  transfer: CryptoCoinTransferStatus;
  borrow: CryptoMarginShortCheck | null;
  index: CryptoCoinIndexStatus;
  updatedAt: string | null;
}

export interface CryptoIndexComponentChange {
  id: number;
  symbol: string;
  exchange: CryptoExchange;
  exchangeName: string;
  component: string;
  oldWeight: number | null;
  newWeight: number | null;
  diff: number | null;
  pushed: boolean;
  pushStatus: string | null;
  pushMessage: string | null;
  createdAt: string;
}

export interface CryptoCoinStatusResponse {
  status: SourceStatus;
  symbol: string;
  exchanges: CryptoExchange[];
  exchangeStatuses: Partial<Record<CryptoExchange, CryptoCoinExchangeStatus>>;
  indexChanges: CryptoIndexComponentChange[];
  indexChangeThreshold: number;
  updatedAt: string | null;
}

export interface CryptoCoinStatusBatchItem {
  symbol: string;
  exchangeStatuses: Partial<Record<CryptoExchange, CryptoCoinExchangeStatus>>;
  indexChanges: CryptoIndexComponentChange[];
}

export interface CryptoCoinStatusBatchResponse {
  status: SourceStatus;
  symbols: string[];
  items: Record<string, CryptoCoinStatusBatchItem>;
  scanning: boolean;
  scanStartedAt: string | null;
  scanFinishedAt: string | null;
  updatedAt: string | null;
}

export interface CryptoPairSpread {
  symbol: string;
  leftExchange: CryptoExchange;
  rightExchange: CryptoExchange;
  leftMarketType: CryptoMarketType;
  rightMarketType: CryptoMarketType;
  pairType: "F" | "S" | string;
  pair: string;
  label: string;
  spread: number | null;
  spreadPct: number | null;
  reverseSpreadPct: number | null;
  bidSpreadPct: number | null;
  askSpreadPct: number | null;
  leftBid: number | null;
  leftAsk: number | null;
  rightBid: number | null;
  rightAsk: number | null;
  leftFundingRate?: number | null;
  rightFundingRate?: number | null;
  leftPremiumRate?: number | null;
  rightPremiumRate?: number | null;
  leftVolume24h?: number | null;
  rightVolume24h?: number | null;
  previousSpreadPct?: number | null;
  spreadJumpPct?: number | null;
  crossedWatch?: boolean;
  crossedStrong?: boolean;
  fundingDiffPct?: number | null;
  premiumDiffPct?: number | null;
  ffSignalLevel?: "none" | "watch" | "strong" | "state" | string | null;
  ffSignalReason?: string | null;
  status: SourceStatus;
  marginShortRequired: boolean;
  marginShortExchange: CryptoExchange | null;
  marginShortSymbol: string | null;
  marginShortStatus: SourceStatus | "not_applicable" | "not_supported" | "not_borrowable" | "pending";
  marginShortMessage: string | null;
  marginShortCheck: CryptoMarginShortCheck | null;
}

export interface CryptoAstroPair extends CryptoPairSpread {
  astroBidSpreadPct: number | null;
  astroAskSpreadPct: number | null;
  localBidSpreadPct: number | null;
  localAskSpreadPct: number | null;
  spreadDiffPct: number | null;
  localMatched: boolean;
  source: string;
  leftFundingRate: number | null;
  rightFundingRate: number | null;
  leftVolume24h: number | null;
  rightVolume24h: number | null;
}

export interface CryptoFundingHistoryItem {
  exchange: CryptoExchange;
  symbol: string;
  fundingRate: number | null;
  premiumRate: number | null;
  markPrice: number | null;
  fundingTime: string | null;
}

export interface CryptoFundingHistoryResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  exchange: CryptoExchange;
  exchangeName: string;
  symbol: string;
  items: CryptoFundingHistoryItem[];
  updated_at: string | null;
  message: string;
}

export interface CryptoFsSignalHistoryItem {
  fundingRate: number | null;
  fundingTime: string | null;
  netFundingRate: number | null;
}

export interface CryptoFsSignalCheck {
  exchange: string;
  status: SourceStatus | "not_supported" | "not_borrowable" | "pending" | "paused";
  message: string | null;
  transferStatus?: CryptoCoinTransferStatus | null;
  canBorrow?: boolean | null;
  inventoryAvailable?: boolean | null;
  borrowableAmount?: number | null;
  borrowableValueUsdt?: number | null;
  hourlyBorrowRate?: number | null;
  dailyBorrowRate?: number | null;
  borrowPeriodRate?: number | null;
  feeRate?: number | null;
  slippageRate?: number | null;
  basisRiskRate?: number | null;
  netFundingRate?: number | null;
  basisRate?: number | null;
  openSpreadRate?: number | null;
  closeSpreadRate?: number | null;
  basisRisk?: "normal" | "watch" | "danger" | "unknown" | string | null;
  basisMessage?: string | null;
  spotBid?: number | null;
  spotAsk?: number | null;
  spotVolume24h?: number | null;
}

export interface CryptoFsBorrowSearchResponse extends CryptoFsSignalCheck {
  symbol: string;
  exchange: CryptoExchange;
  mappedSymbol?: string | null;
  cached: boolean;
  checkedAt: string | null;
  expiresAt: string | null;
  source?: string | null;
}

export interface CryptoFsSignalLog {
  id: number;
  signalKey: string;
  symbol: string;
  futuresExchange: CryptoExchange;
  spotExchange: CryptoExchange;
  fundingTime: string | null;
  currentFundingRate: number | null;
  borrowPeriodRate: number | null;
  netFundingRate: number | null;
  borrowableAmount: number | null;
  borrowableValueUsdt?: number | null;
  status: SourceStatus;
  message: string | null;
  pushed: boolean;
  createdAt: string;
}

export interface CryptoFsSignal {
  signalKey: string;
  symbol: string;
  futuresExchange: CryptoExchange;
  futuresExchanges?: CryptoExchange[];
  futuresRouteCount?: number;
  spotExchange: CryptoExchange;
  fundingTime: string | null;
  currentFundingRate: number | null;
  dailyFundingRate?: number | null;
  premiumRate: number | null;
  volume24h: number | null;
  periodHours?: number | null;
  borrowableAmount?: number | null;
  borrowableValueUsdt?: number | null;
  borrowDailyRate?: number | null;
  borrowHourlyRate?: number | null;
  borrowPeriodRate?: number | null;
  feeRate?: number | null;
  slippageRate?: number | null;
  basisRiskRate?: number | null;
  netFundingRate?: number | null;
  futuresBid?: number | null;
  futuresAsk?: number | null;
  basisRate?: number | null;
  openSpreadRate?: number | null;
  closeSpreadRate?: number | null;
  basisRisk?: "normal" | "watch" | "danger" | "unknown" | string | null;
  basisMessage?: string | null;
  minutesToFunding?: number | null;
  fundingWindowOk?: boolean | null;
  watchOnly?: boolean;
  watchReason?: string | null;
  negativePotential?: boolean;
  potentialType?: "current_negative" | "premium_negative" | string | null;
  potentialReason?: string | null;
  inventoryAvailable?: boolean;
  executableBorrow?: boolean;
  borrowPriority?: number;
  largeSpread?: boolean;
  limitOrderCandidate?: boolean;
  spreadRate?: number | null;
  limitOrderReason?: string | null;
  checks: Partial<Record<CryptoExchange, CryptoFsSignalCheck>>;
  historyFunding: CryptoFsSignalHistoryItem[];
  actionable: boolean;
  reason: string | null;
  pushed: boolean;
  pushStatus: SourceStatus | "skipped" | null;
  pushMessage: string | null;
  lastLog: CryptoFsSignalLog | null;
}

export interface CryptoFsSignalsResponse {
  status: SourceStatus;
  updatedAt: string | null;
  source: string;
  threshold: number;
  feeRate?: number | null;
  slippageRate?: number | null;
  basisWatchRiskRate?: number | null;
  basisDangerRiskRate?: number | null;
  cooldownMinutes: number;
  watchThreshold?: number | null;
  watchDailyThreshold?: number | null;
  candidateCount: number;
  actionableCount: number;
  watchCount?: number;
  pushedCount: number;
  hiddenUnavailableCount?: number;
  potentialCount?: number;
  borrowableCount?: number;
  unavailableCount?: number;
  largeSpreadNoBorrowCount?: number;
  exchangeChecks: Partial<Record<CryptoExchange, CryptoFsSignalCheck>>;
  items: CryptoFsSignal[];
  watchItems?: CryptoFsSignal[];
  scanning?: boolean;
  scanStartedAt?: string | null;
  scanFinishedAt?: string | null;
  message?: string | null;
  astroAutoCard?: {
    enabled: boolean;
    configured: boolean;
    dryRun: boolean;
    state: "disabled" | "not_configured" | "dry_run" | "syncing" | "ready" | "idle" | "queued" | string;
    message: string;
    baseUrl?: string | null;
    adminPrefix?: string | null;
    defaultPaused: boolean;
    defaultDisableOpen: boolean;
    maxCardsPerScan: number;
    unlimitedCardsPerScan?: boolean;
    defaultLeverage?: number;
    defaultMinNotionalUsdt?: number;
    defaultMaxNotionalUsdt?: number;
    defaultGreaterPriceAlertPct?: number | null;
    defaultPriceChangeAlertPct?: number | null;
    defaultPriceChangeAlertOnlyRise?: boolean;
    verificationMode?: string;
    verificationPollSeconds?: number;
    verificationTimeoutSeconds?: number;
    sdkReadTotalSeconds?: number;
    pendingSubmissionCount?: number;
    pendingSubmissions?: {
      count: number;
      waitingCount?: number;
      reviewCount?: number;
      checkScheduleSeconds?: number[];
      recentResolutions?: { name: string; type: string; buyEx: string; sellEx: string; submissionId?: string; state: string; resolution: string; resolvedAt: string; cardId?: string }[];
      policy: string;
      items: { name: string; type: string; buyEx: string; sellEx: string; state: string; submittedAt: string; updatedAt: string; error?: string | null;
        submissionId?: string; checkCount?: number; lastCheckedAt?: string; lastCheckOutcome?: string; lastCheckError?: string; reviewReason?: string }[];
    };
    dexConfiguration?: {
      enabled: boolean;
      mode: "signed_sdk" | "unavailable" | string;
      message: string;
    };
    automaticCleanup?: {
      enabled: boolean;
      graceSeconds: number;
      continuousInvalidSeconds: number;
      systemDeleteCooldownSeconds: number;
      invalidTrackingCount: number;
      cooldownCount: number;
      safetyRule: string;
      configSnapshotCoverage?: {
        legacyProtected: number;
        submittedOnlyProtected: number;
        unreadableSubmittedFields: string[];
      };
    };
    spreadScanner?: {
      newsPolicy?: {
        running: boolean; intervalSeconds: number; lastReadAt: string | null; sourceUpdatedAt: string | null;
        lastError: string | null; storageError: string | null; blockCount: number; unresolvedNoticeCount: number;
        listingCardMode?: string; listingScheduleError?: string | null; lastListingScheduleAt?: string | null;
        listingRoutes?: Array<{ symbol: string; type: string; buyExchange: string; sellExchange: string; category: string; state: string; announcements: string[] }>;
        listingSymbols: string[]; lastCardCheckAt: string | null; cardCheckError: string | null;
        blocks: Array<{ symbol: string; exchange: string; market: string; sourceUrl: string; scheduledAt?: string }>;
        cardChecks: Array<{ id: string; symbol: string; type: string; buyExchange: string; sellExchange: string; status: string; error?: string }>;
      };
      fundingRead?: { enabled: boolean; scope: string; rule: string };
      enabled: boolean;
      running: boolean;
      source: string;
      intervalSeconds: number;
      minOpenSpreadPct: number;
      maxOpenSpreadPct: number;
      minVolumeUsdt: number;
      deleteRearmPct: number;
      deletePullbackPctPoints: number;
      deleteRearm?: {
        rearmPct: number;
        pullbackPctPoints: number;
        activeGuardCount: number;
        waitingPullbackCount: number;
        waitingRetriggerCount: number;
        pendingDeletionCount: number;
        items: {
          name: string;
          type: string;
          buyEx: string;
          sellEx: string;
          deletionDetectedAt: string;
          deletionReferenceOpenPosition: number;
          rearmOpenPosition: number;
          pullbackRequiredPctPoints: number;
          observedPullbackPctPoints?: number | null;
          rearmPullbackObservedAt?: string | null;
          rearmPullbackConfirmedAt?: string | null;
          state: 'waiting_for_pullback_or_direct_breakout' | 'waiting_for_normal_retrigger';
        }[];
      };
      blockedPairs: {
        marketKey: string;
        symbol: string;
      }[];
      blockedCoins: string[];
      dexMappedAssets: {
        exchange?: string;
        autoCreateEligible?: boolean;
        symbol: string;
        chainIndex: string;
        chainLabel: string;
        contractAddress: string;
      }[];
      confirmations: number;
      maxQuoteAgeSeconds: number;
      greaterPriceAlertPct?: number | null;
      priceChangeAlertPct?: number | null;
      priceChangeAlertOnlyRise?: boolean;
      minNotionalUsdt?: number;
      maxNotionalUsdt?: number;
      hotMonitor?: {
        enabled: boolean;
        running?: boolean;
        intervalMs: number;
        routeTtlSeconds: number;
        hitEvidenceTtlSeconds: number;
        workers: number;
        routeCount?: number;
        aboveThresholdRouteCount?: number;
        newListingRouteCount?: number;
        lastCheckAt?: string | null;
        lastHitAt?: string | null;
        lastError?: string | null;
        triggerSources: string[];
        createRule: string;
        pulseCanCreateDirectly: boolean;
        inFlightRouteCount?: number;
        fundingDepthSkippedCount?: number;
        listingProbeRouteCount?: number;
        listingProbeMaxConcurrent?: number;
        listingProbeIntervalSeconds?: number;
        maxRouteWaitSinceLastStartMs?: number;
        routeWaits?: {
          symbol: string;
          type: string;
          buyExchange: string;
          sellExchange: string;
          registeredAtMs: number;
          firstDirectCheckStartedAtMs?: number | null;
          lastDirectCheckStartedAtMs?: number | null;
          lastSelectedAtMs?: number | null;
          inFlight: boolean;
          listingProbe?: boolean;
          checks: number;
          waitSinceLastStartMs: number;
          pollIntervalMs?: number;
          lastDirectDurationMs?: number | null;
          fundingWaiting?: boolean;
          priceBackoffCount?: number;
        }[];
      };
      finalRevalidation?: {
        enabled: boolean;
        workers?: number;
        maxQuoteAgeSeconds: number;
        okxDexMaxQuoteAgeSeconds?: number;
        okxDexSubmitMaxQuoteAgeSeconds?: number;
        okxDexDistinctWaitSeconds?: number;
        okxDexPollSeconds?: number;
        maxQuoteSkewSeconds: number;
        okxDexTimestampSkewCheck?: boolean;
        timeoutSeconds: number;
        rounds: number;
        intervalMs: number;
        source: string;
        rule: string;
      };
      revalidationRetry?: {
        workers: number;
        defaultRetrySeconds: number;
        quoteRetrySeconds: number;
        unavailableRetrySeconds: number;
        earlyRetryImprovementPctPoints: number;
        activeCooldownCount: number;
        metricsWindowStartedAt?: string | null;
        windowAttempted: number;
        windowPassed: number;
        windowFailed: number;
        windowPassRatePct: number;
        windowReasons: Record<string, number>;
      };
      apiDegradedMode?: {
        unverifiedCardAllowed?: boolean;
        localDeadlineSeconds?: number;
        localFaultPushEnabled?: boolean;
        cloudFaultPushEnabled?: boolean;
        routeControl?: {
          faultDelaySeconds: number;
          globalPushIntervalSeconds: number;
          recoveryStableSeconds: number;
          quoteQualityPushEnabled?: boolean;
          affectedRouteCount: number;
          cloudRouteCount?: number;
          awaitingRecheckRouteCount?: number;
          cloudQueue: { active: number; waiting: number; maxActive: number; maxWaiting: number; coalesced: number; expired: number };
          routes: { route: string; symbol: string; type: string; buyExchange: string; sellExchange: string;
            mode: string; category?: string | null; error?: string | null; lastReason?: string;
            durationSeconds: number; evidenceFresh: boolean; lastSuccessfulVerificationAt?: string | null;
            lastCheckedAt?: string | null; lastDurationMs?: number | null }[];
        };
        cloudBackup?: {
          enabled: boolean;
          state: string;
          activeSources: string[];
          minDwellSeconds: number;
          lastError?: string | null;
          lastDurationMs?: number;
          attempted?: number;
          passedObservations?: number;
          failedObservations?: number;
        };
        active: boolean;
        incidents: {
          source: string;
          startedAt?: string | null;
          lastFailureAt?: string | null;
          failureCount: number;
          affectedSymbols: string[];
          lastError?: string | null;
        }[];
        affectedSources: string[];
        affectedSymbols: string[];
        confirmedRouteFailureCount: number;
        failureThreshold: number;
        pushIntervalSeconds: number;
        recoveryProbe: {
          running: boolean;
          activeOnly: boolean;
          intervalSeconds: number;
          requiredConsecutiveSuccesses: number;
          sources: Record<string, {
            attemptCount: number;
            consecutiveSuccesses: number;
            lastProbeAt?: string | null;
            lastProbeDurationMs?: number | null;
            lastProbeError?: string | null;
            recoveryConfirmedAt?: string | null;
          }>;
        };
        fallbackCardCount: number;
        lastFallbackCardAt?: string | null;
        lastPushAt?: string | null;
        lastPushStatus?: string | null;
        lastPushMessage?: string | null;
        rule: string;
      };
      pulseSources?: {
        configuredCount: number;
        successCount: number;
        failureCount: number;
        failures: { url: string; error: string }[];
        degraded: boolean;
        rule: string;
      };
      pairTypes: string[];
      autoCardRules?: {
        ff: {
          minOpenSpreadPctExclusive: number;
          exchanges: string[];
          buyExchanges?: string[];
          sellExchanges?: string[];
          bybitSellException?: { enabled: boolean; minOpenSpreadPctExclusive: number };
          gcCompanion: string;
          structureFilter?: {
            enabled: boolean;
            historyHours: number;
            sampleSeconds: number;
            trackingMinSpreadPct: number;
            adverseFundingShare: number;
            maxFiveMinuteIncreasePct: number;
            missingHistoryRejected: boolean;
            gateIndexEvidence: string;
            rule: string;
          };
        };
        sf: {
          pancakeswapV3Enabled?: boolean;
          dexMinOpenSpreadPctExclusive?: {okxdex: number; pancakeswapv3: number};
          fundingRule?: {minimumRatePct: number; exemptSpreadPct: number; unknownAction: string};
          minOpenSpreadPctExclusive: number;
          spotExchanges: string[];
          okxDexRoute?: {
            buyExchange: "okxdex" | string;
            autoCardEnabled?: boolean;
            sellExchanges: string[];
            dexConfigurationRequired: boolean;
            dexConfigurationApiReady: boolean;
            mappingMode?: string;
            mappingRule?: string;
            mappedAssetCount?: number;
            mappedAssets?: {
              symbol: string;
              chainIndex: string;
              chainLabel: string;
              contractAddress: string;
            }[];
            unmappedCandidateCount?: number;
            unmappedItems?: {
              exchange?: string;
              symbol: string;
              chainIndex: string;
              chainLabel: string;
              contractAddress: string;
              targetExchanges: string[];
              maxOpenSpreadPct?: number | null;
              maxVolume24hUsdt?: number | null;
              reason: string;
            }[];
            slippagePct: number;
            identityVerificationRequired: boolean;
            identitySource: string;
            identityRule: string;
            targetSelectionRule?: string;
            verifiedCandidateCount: number;
            blockedCandidateCount: number;
            statusCounts: Record<string, number>;
            lastError?: string | null;
          };
          excludedFuturesExchanges: string[];
          minShortFundingRatePct: number;
          negativeFundingOverride?: {
            enabled: boolean;
            minimumExecutableSpreadPct: number;
            fundingCostHorizonHours: number;
            minimumNetSpreadAfterFundingPct: number;
          };
          missingFundingRejected: boolean;
          gcCompanion: string;
        };
        fsBorrow?: {
          enabled: boolean;
          futuresExchanges: string;
          spotExchange: string;
          spotMarginType: string;
          leverage: number;
          inventoryRequired: boolean;
          borrowRateRequired: boolean;
          minCycleProfitPctExclusive: number;
          minOpenSpreadPctExclusive: number;
          cycleProfitFormula: string;
          spreadFormula: string;
        };
      };
      delistingRule?: {
        enabled: boolean;
        rule: string;
        activeBlockCount: number;
        filteredCandidateCount: number;
        items: { symbol: string; exchange: string }[];
        lastError?: string | null;
      };
      exchanges: string[];
      subscriptions: string[];
      subscriptionSource: "saved" | "environment" | "legacy_environment" | "default" | string;
      availableMarkets: {
        key: string;
        exchange: string;
        exchangeName: string;
        marketType: "spot" | "future";
        enabled: boolean;
      }[];
      lastScanAt?: string | null;
      lastScanDurationMs?: number | null;
      lastError?: string | null;
      marketCount?: number;
      candidateCount?: number;
      confirmedCount?: number;
      structureBlockedCount?: number;
      structureWarmingCount?: number;
      structureHistory?: {
        path: string | null;
        routeCount: number;
        sampleCount: number;
        oldestAt: string | null;
      };
    };
  };
}

export interface CryptoFsObservationBand {
  key: string;
  label: string;
  count: number;
  borrowableCount: number;
  borrowableRate: number | null;
}

export interface CryptoFsObservationEvent {
  type: "borrow_lost" | "borrow_recovered";
  symbol: string;
  futuresExchange: CryptoExchange;
  dailyFundingRate: number | null;
  borrowableAmount: number | null;
  createdAt: string;
}

export interface CryptoFsObservationSummary {
  status: SourceStatus;
  days: number;
  updatedAt: string;
  observationCount: number;
  symbolCount: number;
  currentCandidateCount: number;
  currentBorrowableCount: number;
  currentLargeSpreadNoBorrowCount: number;
  borrowLostCount: number;
  borrowRecoveredCount: number;
  bands: CryptoFsObservationBand[];
  topSymbols: { symbol: string; observationCount: number }[];
  recentEvents: CryptoFsObservationEvent[];
  insights: string[];
}

export interface CryptoFundingCapExchangeSnapshot {
  exchange: CryptoExchange;
  exchangeName: string;
  status: SourceStatus | "unknown" | "not_supported";
  message: string | null;
  maxFundingRate: number | null;
  minFundingRate: number | null;
  fundingIntervalHours: number | null;
  checkedAt: string | null;
}

export interface CryptoFundingCapWatchItem {
  id: number;
  symbol: string;
  enabled: boolean;
  lastCheckedAt: string | null;
  lastError: string | null;
  createdAt: string;
  selectedExchanges: CryptoExchange[];
  exchanges: CryptoFundingCapExchangeSnapshot[];
}

export interface CryptoFundingCapEvent {
  id: number;
  symbol: string;
  exchange: CryptoExchange;
  exchangeName: string;
  previousMaxFundingRate: number | null;
  currentMaxFundingRate: number | null;
  previousMinFundingRate: number | null;
  currentMinFundingRate: number | null;
  fundingIntervalHours: number | null;
  previousFundingIntervalHours: number | null;
  currentFundingIntervalHours: number | null;
  changeKinds: ("cap" | "interval")[];
  pushed: boolean;
  pushStatus: SourceStatus | "manual_only" | null;
  pushMessage: string | null;
  createdAt: string;
}

export interface CryptoFundingCapWatchResponse {
  status: SourceStatus;
  monitoring: boolean;
  itemCount: number;
  exchanges: { code: CryptoExchange; name: string }[];
  items: CryptoFundingCapWatchItem[];
  recentEvents: CryptoFundingCapEvent[];
  scan: {
    running: boolean;
    startedAt: string | null;
    finishedAt: string | null;
    lastError: string | null;
    checkedSymbolCount: number;
    changedCount: number;
    pushedCount: number;
    status: SourceStatus | "idle" | "running";
    message: string;
    durationSeconds?: number | null;
  };
  message: string;
}

export interface CryptoFundingFormationTarget {
  key: "cap" | "floor" | "custom" | string;
  label: string;
  targetFundingRate: number;
  status: "ok" | "estimated" | "band" | "impossible" | "invalid" | "insufficient_history" | "settled";
  relation?: "gte" | "lte" | "band";
  premiumBoundary?: number;
  premiumBoundaryLow?: number;
  premiumBoundaryHigh?: number;
  baseFundingRate?: number;
  estimatedRequiredPremiumRate?: number;
  requiredPremiumRate?: number;
  requiredPremiumRangeLow?: number;
  requiredPremiumRangeHigh?: number;
  conservative?: boolean;
  reached?: boolean;
  calculatedFundingRate?: number;
  message?: string;
}

export interface CryptoFundingFormationResponse {
  status: SourceStatus;
  exchange: CryptoExchange;
  exchangeName: string;
  symbol: string;
  currentFundingRate: number | null;
  latestPremiumRate: number | null;
  latestPremiumCandleTime?: string | null;
  latestPremiumKind?: string;
  stale?: boolean;
  staleAgeSeconds?: number;
  recentPremiumMedianRate?: number | null;
  recentPremiumSampleCount?: number;
  robustPredictedFundingRate?: number | null;
  predictionSensitivityLow?: number | null;
  predictionSensitivityHigh?: number | null;
  fundingIntervalHours: number;
  nextFundingTime: string;
  cycleStartTime: string;
  historyWindowStartTime: string;
  historyWindowEndTime: string;
  minutesToFunding: number;
  maxFundingRate: number | null;
  minFundingRate: number | null;
  interestRate: number;
  intervalScale: number;
  weighting: "equal" | "linear";
  officialSampleSeconds: number;
  formulaVersion: string;
  ruleWindowMode: "settlement_cycle" | "rolling_interval_reference" | string;
  aggregationMode: "average_premium_then_formula" | "average_sample_funding" | string;
  formula: string;
  formulaType: string | null;
  ruleSourceUrl: string;
  historySource: string;
  publicResolutionSeconds: number;
  historyPrecision: string;
  premiumAverageHistory?: {
    timestamp: string;
    averagePremiumRate: number;
  }[];
  effectiveFundingFloor: number | null;
  effectiveFundingCap: number | null;
  rollingReference: {
    windowStartTime: string;
    windowEndTime: string;
    sampleCount: number;
    expectedSampleCount: number;
    coverage: number;
    averagePremiumRate: number | null;
    calculatedFundingRate: number | null;
    historyStatus: "complete" | "estimated" | "insufficient";
  } | null;
  shadowCalculation: {
    status: "estimated" | "insufficient";
    formulaVersion: string;
    predictedFundingRate: number | null;
    predictionCapMode?: "uncapped_observation" | string;
    predictionCapApplied?: boolean;
    calculatedFundingRate: number | null;
    coverage: number;
  } | null;
  accuracyStatus: "official" | "bounded" | "estimated" | "insufficient";
  accuracyMessage: string;
  updatedAt: string;
  totalSamples: number;
  elapsedSamples: number;
  remainingSamples: number;
  coveredSamples: number;
  coverage: number;
  weightedCoverage: number;
  completeHistory: boolean;
  historyStatus: "complete" | "estimated" | "insufficient";
  averagePremiumRate: number | null;
  averagePremiumLow: number | null;
  averagePremiumHigh: number | null;
  calculatedFundingRate: number | null;
  predictedAveragePremiumRate: number | null;
  predictedFundingRate: number | null;
  futurePremiumAnchorRate: number | null;
  predictionStatus: "estimated" | "settled" | "insufficient";
  predictionMethod: "shrunk_latest_premium_carry_forward_v2" | string;
  predictionModelVersion: string;
  predictionCapMode: "uncapped_observation" | string;
  predictionCapApplied: boolean;
  predictionConfidence: "high" | "medium" | "low" | "scenario" | "settled";
  predictionLatestPremiumWeight: number;
  predictionMessage: string;
  targets: CryptoFundingFormationTarget[];
  computeLocation?: "tencent_cloud" | "local" | string;
  cloudUpdatedAt?: string;
}

export interface CryptoFundingFormationBatchItem {
  id: string;
  status: "ok" | "error";
  data: CryptoFundingFormationResponse | null;
  error: string | null;
}

export interface CryptoFundingFormationBatchResponse {
  status: SourceStatus;
  updatedAt: string;
  itemCount: number;
  errorCount: number;
  items: CryptoFundingFormationBatchItem[];
  computeLocation?: "tencent_cloud" | "local" | string;
  cloudUpdatedAt?: string;
}

export interface CryptoFundingCloudStatus {
  enabled: boolean;
  status: "ok" | "error" | "disabled" | string;
  computeLocation: "tencent_cloud" | "local_paused" | string;
  version?: string;
  watchCount?: number;
  totalWatchCount?: number;
  disabledWatchCount?: number;
  predictionCount?: number;
  predictionRetentionDays?: number;
  batchWorkers?: number;
  peakRssMiB?: number;
  cloudUpdatedAt?: string;
  error?: string;
  cache?: {
    itemCount: number;
    maxItems: number;
    freshSeconds: number;
    staleFallbackSeconds: number;
  };
}

export interface CryptoFundingFormationWatchResponse {
  status: SourceStatus;
  updatedAt: string;
  itemCount: number;
  items: Array<{
    exchange: CryptoExchange;
    symbol: string;
    targetRate: number | null;
    lastCheckedAt: string | null;
    lastError: string | null;
    health?: {
      status: string;
      message: string;
      lastSuccessfulAt: string | null;
      nextCheckpointAt: string | null;
      nextCheckpointMinutes: number | null;
      lastRecordedAt: string | null;
      missedCount: number;
      error: string | null;
    };
  }>;
}

export interface CryptoFundingPredictionReviewItem {
  settlementPredictedRate: number;
  boundsStatus: "complete" | "missing" | "partial" | "invalid";
  includedInAccuracy: boolean;
  scoreCorrected: boolean;
  originalEvaluation: { at: string; message: string; success: boolean; absoluteError: number } | null;
  id: number;
  exchange: CryptoExchange;
  symbol: string;
  settlementTime: string;
  checkpointMinutes: number;
  predictedAt: string;
  leadSeconds: number;
  systemPredictedRate: number;
  exchangePredictedRate: number | null;
  actualFundingRate: number | null;
  absoluteError: number | null;
  directionHit: boolean | null;
  success: boolean | null;
  evaluationStatus: "pending" | "matched" | "unavailable";
  evaluationError: string | null;
  coverage: number | null;
  formulaVersion: string | null;
  predictionModelVersion: string | null;
  shadowFormulaVersion: string | null;
  shadowPredictedRate: number | null;
  shadowAbsoluteError: number | null;
  shadowSuccess: boolean | null;
  calculationDetails: Record<string, unknown>;
}

export interface CryptoFundingPredictionFormulaBreakdown {
  exchange: CryptoExchange;
  formulaVersion: string;
  checkpointMinutes: number;
  sampleCount: number;
  hitCount: number;
  hitRate: number;
  meanAbsoluteError: number | null;
  shadow: boolean;
}

export interface CryptoFundingPredictionReviewResponse {
  scoredCount: number;
  legacyCount: number;
  legacyHitRate: number | null;
  correctedLogCount: number;
  totalRecordCount: number;
  detailLimit: number;
  status: SourceStatus;
  updatedAt: string;
  windowDays: number;
  checkpointMinutes: number;
  modelVersion: string;
  activeModelVersion: string;
  availableCheckpoints: number[];
  successDefinition: string;
  sampleStatus: "ok" | "insufficient";
  settledCount: number;
  pendingCount: number;
  unavailableCount: number;
  hitCount: number;
  hitRate: number | null;
  directionHitRate: number | null;
  meanAbsoluteError: number | null;
  medianAbsoluteError: number | null;
  meanSignedError: number | null;
  exchangeHitRate: number | null;
  exchangeMeanAbsoluteError: number | null;
  breakdown: CryptoFundingPredictionFormulaBreakdown[];
  shadowBreakdown: CryptoFundingPredictionFormulaBreakdown[];
  items: CryptoFundingPredictionReviewItem[];
  computeLocation?: "tencent_cloud" | "local" | string;
  cloudUpdatedAt?: string;
}

export interface CryptoPairSpreadItem {
  time: string;
  leftPrice: number | null;
  leftBid: number | null;
  leftAsk: number | null;
  rightPriceRaw: number | null;
  rightPrice: number | null;
  rightBidRaw: number | null;
  rightAskRaw: number | null;
  rightBid: number | null;
  rightAsk: number | null;
  spreadAbs: number | null;
  spreadPct: number | null;
  openSpreadPct: number | null;
  closeSpreadPct: number | null;
  rawRatio: number | null;
  source: "candle" | "ticker" | string;
  dataQuality?: "direct" | "backfilled_1m" | string;
  fxRate?: number | null;
  standardPremiumPct?: number | null;
}

export interface CryptoPairSpreadDataQuality {
  coverageStart: string | null;
  coverageEnd: string | null;
  coverageHours: number;
  commonCandleCount: number;
  expectedCandleCount: number;
  missingCandleCount: number;
  backfilledCandleCount: number;
  completenessPct: number | null;
  listingLimited: boolean;
}

export interface CryptoPairSpreadResponse {
  status: SourceStatus;
  source: string;
  exchange: CryptoExchange | "multi";
  leftExchange: CryptoExchange;
  rightExchange: CryptoExchange;
  leftExchangeName: string;
  rightExchangeName: string;
  leftSymbol: string;
  rightSymbol: string;
  leftMarketSymbol: string;
  rightMarketSymbol: string;
  leftPriceRatio: number;
  rightPriceRatio: number;
  rightRatio: number;
  granularity: string;
  rangeHours: number;
  updatedAt: string | null;
  latest: CryptoPairSpreadItem;
  items: CryptoPairSpreadItem[];
  dataQuality: CryptoPairSpreadDataQuality;
  message: string;
}

export interface SkHynixAdrCitiStatus {
  status: "ok" | "partial" | "error";
  sourceName: string;
  sourceUrl: string;
  bookStatus?: "open" | "closed" | "unknown";
  issuanceOpen?: boolean;
  scheduledOpenDate?: string | null;
  ratioOrd?: number;
  ratioDr?: number;
  crossCheckStatus?: "confirmed" | "partial" | "conflict";
  noticeIds?: string[];
  noticeFingerprint?: string;
  latestNoticeUrl?: string;
  announcedOpenText?: string;
  verification?: {
    status: "ok" | "error";
    sourceName?: string;
    sourceUrl?: string;
    bookStatus?: "open" | "closed" | "unknown";
    issuanceOpen?: boolean;
    ordIsin?: string | null;
    checkedAt?: string;
    error?: string | null;
  };
  generatedAtText?: string | null;
  checkedAt: string;
  cached?: boolean;
  error?: string | null;
}

export interface SkHynixAdrKsdStatus {
  status: "ok" | "error";
  sourceName: string;
  sourceUrl: string;
  detailUrl?: string;
  company?: string;
  isin?: string;
  convertibleCommonShares?: number;
  convertibleAds?: number;
  referenceDate?: string | null;
  checkedAt: string;
  cached?: boolean;
  error?: string | null;
}

export interface SkHynixAdrMonitorResponse {
  status: "ok" | "partial" | "error";
  checkedAt: string;
  refreshSeconds: number;
  externalMonitorIntervalMinutes: number;
  pollingPlan: {
    mode: "normal" | "high_frequency";
    phase: "upcoming" | "active" | "passed";
    intervalSeconds: number;
    announcedOpenText: string;
    windowKey: string;
    windowStart: string;
    windowEnd: string;
    windowBeijingText: string;
    note: string;
  };
  backgroundMonitor: {
    status: "ok";
    settings: {
      enabled: boolean;
      pushEnabled: boolean;
      intervalSeconds: number;
      failurePushAfter: number;
    };
    scheduler: {
      running: boolean;
      scanning: boolean;
      intervalSeconds: number;
      lastScanAt: string | null;
      lastSuccessAt: string | null;
      lastError: string | null;
      activeIntervalSeconds?: number;
      pollingMode?: "normal" | "high_frequency";
    };
    pollingPlan: SkHynixAdrMonitorResponse["pollingPlan"];
    barkStatus: "ok" | "not_configured" | "error" | string;
    state: {
      lastProcessedChangeId: string | null;
      conversionAvailable: boolean | null;
      conversionAlerted: boolean;
      lastPushAt: string | null;
      lastPushStatus: string | null;
      lastPushMessage: string | null;
      lastAlert: Record<string, unknown> | null;
      sourceFailures: Record<string, unknown>;
    };
    recentScans: Array<{
      scanAt: string;
      finishedAt: string;
      durationMs: number;
      status: "ok" | "partial" | "error" | "disabled" | string;
      citi: {
        status?: "ok" | "partial" | "error" | string;
        issuanceOpen?: boolean | null;
        checkedAt?: string | null;
        cached?: boolean;
        error?: string | null;
      };
      citiCrossCheckStatus?: "confirmed" | "partial" | "conflict" | null;
      pollingMode?: "normal" | "high_frequency" | null;
      pollingIntervalSeconds?: number | null;
      officialClues?: {
        status?: "ok" | "partial" | "error" | string;
        itemCount?: number;
        checkedAt?: string | null;
      };
      ksd: {
        status?: "ok" | "error" | string;
        convertibleCommonShares?: number | null;
        checkedAt?: string | null;
        cached?: boolean;
        error?: string | null;
      };
      decision: {
        level?: "available" | "blocked" | "unknown" | string;
        canRequestConversion: boolean;
        title?: string | null;
        reason?: string | null;
      };
      bark: {
        eligibleTrigger: boolean;
        events: Array<{
          type?: string | null;
          title?: string | null;
          status?: string | null;
          message?: string | null;
        }>;
      };
      error?: string | null;
    }>;
  };
  program: {
    company: string;
    ticker: string;
    cusip: string;
    isin: string;
    conversionDirection: string;
  };
  decision: {
    level: "available" | "blocked" | "unknown";
    canRequestConversion: boolean;
    title: string;
    reason: string;
  };
  citi: SkHynixAdrCitiStatus;
  ksd: SkHynixAdrKsdStatus;
  officialClues: {
    status: "ok" | "partial" | "error";
    checkedAt: string;
    fingerprint: string;
    note: string;
    items: Array<{
      id: string;
      source: string;
      kind: string;
      publishedAt?: string | null;
      title: string;
      url: string;
    }>;
    sources: Array<{
      status: "ok" | "error";
      sourceName: string;
      sourceUrl: string;
      checkedAt: string;
      error?: string | null;
    }>;
  };
  recentChanges: Array<{
    changedAt: string;
    summary: string;
    snapshot: Record<string, unknown>;
  }>;
  officialLinks: Array<{
    key: string;
    label: string;
    url: string;
    mode: string;
  }>;
  note: string;
}

export interface SkHynixAdrBarkTestResponse {
  status: string;
  barkStatus: string;
  message: string | null;
}

export interface CryptoFsSchedulerStatus {
  status: SourceStatus;
  enabled: boolean;
  running: boolean;
  scanning: boolean;
  intervalSeconds: number;
  limit: number;
  lastScanStartedAt?: string | null;
  lastScanAt?: string | null;
  lastScanDurationSeconds?: number | null;
  nextScanAt?: string | null;
  lastError?: string | null;
}

export interface CryptoCompoundOpenSignal {
  id: number;
  signalKey: string;
  strategy: "FF" | "SF" | string;
  symbol: string;
  leftExchange: CryptoExchange;
  rightExchange: CryptoExchange;
  leftMarketType: CryptoMarketType;
  rightMarketType: CryptoMarketType;
  openedAt: string;
  openSpreadPct: number | null;
  previousSpreadPct: number | null;
  spreadJumpPct: number | null;
  fundingDiffPct: number | null;
  premiumDiffPct: number | null;
  leftVolume24h: number | null;
  rightVolume24h: number | null;
  reason: string | null;
  review30mSpreadPct: number | null;
  review30mConvergencePct: number | null;
  review30mAt: string | null;
  review60mSpreadPct: number | null;
  review60mConvergencePct: number | null;
  review60mAt: string | null;
  status: SourceStatus | "open" | "reviewed" | string;
  pushed: boolean;
  pushStatus: SourceStatus | "skipped" | null;
  pushMessage: string | null;
  createdAt: string;
}

export interface CryptoCompoundOpenSignalsResponse {
  status: SourceStatus;
  updatedAt: string | null;
  threshold: number;
  jumpThreshold: number;
  cooldownMinutes: number;
  scanSeconds: number;
  scanning: boolean;
  scanStartedAt: string | null;
  scanFinishedAt: string | null;
  checkedCount: number | null;
  createdCount: number | null;
  items: CryptoCompoundOpenSignal[];
}

export interface CryptoBorrowWatchCheck {
  id?: number;
  symbol: string;
  exchange: CryptoExchange;
  status: SourceStatus | "not_supported" | "not_borrowable" | "pending" | "skipped";
  message: string | null;
  canBorrow: boolean | null;
  borrowableAmount: number | null;
  borrowableValueUsdt: number | null;
  hourlyBorrowRate: number | null;
  dailyBorrowRate: number | null;
  pushed: boolean;
  pushStatus: SourceStatus | "skipped" | null;
  pushMessage: string | null;
  createdAt: string | null;
}

export interface CryptoBorrowWatchItem {
  id: number;
  symbol: string;
  exchanges: CryptoExchange[];
  enabled: boolean;
  cooldownMinutes: number;
  lastCheckedAt: string | null;
  updatedAt: string | null;
  checks: Partial<Record<CryptoExchange, CryptoBorrowWatchCheck>>;
}

export interface CryptoBorrowWatchResponse {
  status: SourceStatus;
  updatedAt: string | null;
  defaultExchanges: CryptoExchange[];
  refreshSeconds: number;
  cooldownMinutes: number;
  scanning: boolean;
  scanStartedAt: string | null;
  scanFinishedAt: string | null;
  itemCount: number;
  borrowableCount: number;
  pushedCount: number;
  items: CryptoBorrowWatchItem[];
}

export type CryptoMonitorEventType =
  | "borrow_available"
  | "transfer_blocked"
  | "transfer_recovered"
  | "index_component_changed"
  | "ff_compound_signal";

export interface CryptoMonitorEvent {
  id: number;
  symbol: string;
  exchange: CryptoExchange;
  exchangeName: string;
  eventType: CryptoMonitorEventType;
  severity: "opportunity" | "risk" | "change" | "info" | string;
  title: string;
  body: string;
  details: Record<string, unknown>;
  pushed: boolean;
  pushStatus: SourceStatus | "skipped" | null;
  pushMessage: string | null;
  acknowledgedAt: string | null;
  createdAt: string;
}

export interface CryptoMonitorEventsResponse {
  status: SourceStatus;
  updatedAt: string | null;
  unreadCount: number;
  items: CryptoMonitorEvent[];
}

export interface CryptoWatchHistoryPoint {
  time: string;
  spread: number | null;
  spreadPct: number | null;
  bidSpreadPct: number | null;
  askSpreadPct: number | null;
  leftBid: number | null;
  leftAsk: number | null;
  rightBid: number | null;
  rightAsk: number | null;
  leftFundingRate: number | null;
  rightFundingRate: number | null;
  leftPremiumRate: number | null;
  rightPremiumRate: number | null;
  leftMarkPrice: number | null;
  rightMarkPrice: number | null;
  leftIndexPrice: number | null;
  rightIndexPrice: number | null;
  status: SourceStatus;
}

export interface CryptoWatchHistoryResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  message: string;
  item: CryptoWatchItem;
  hours: number;
  points: CryptoWatchHistoryPoint[];
  leftFundingHistory: CryptoFundingHistoryItem[];
  rightFundingHistory: CryptoFundingHistoryItem[];
  fundingErrors: string[];
}

export interface CryptoOrderBookLevel {
  side: "bid" | "ask" | string;
  level: number;
  price: number | null;
  size: number | null;
  notional: number | null;
}

export interface CryptoOrderBook {
  exchange: CryptoExchange;
  exchangeName: string;
  symbol: string;
  marketType: CryptoMarketType;
  amountUnit: string;
  turnover4hUsdt: number | null;
  bids: CryptoOrderBookLevel[];
  asks: CryptoOrderBookLevel[];
  updatedAt: string | null;
  status: SourceStatus;
  lastError: string | null;
}

export interface CryptoPushRule {
  id: number | null;
  watchItemId: number;
  enabled: boolean;
  openSpreadPct: number | null;
  closeSpreadPct: number | null;
  premiumDiffPct: number | null;
  cooldownMinutes: number;
  lastTriggeredAt: string | null;
  updatedAt: string | null;
}

export interface CryptoPushRulePayload {
  enabled?: boolean;
  openSpreadPct?: number | null;
  closeSpreadPct?: number | null;
  premiumDiffPct?: number | null;
  cooldownMinutes?: number | null;
}

export interface CryptoPushLog {
  id: number;
  watchItemId: number;
  eventType: string;
  title: string;
  body: string;
  spreadPct: number | null;
  premiumDiffPct: number | null;
  status: SourceStatus;
  message: string | null;
  createdAt: string;
}

export interface CryptoPushTestResponse {
  status: SourceStatus;
  message: string | null;
  log: CryptoPushLog;
}

export interface CryptoWatchOrderBooksResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  message: string;
  item: CryptoWatchItem;
  leftOrderBook: CryptoOrderBook;
  rightOrderBook: CryptoOrderBook;
}

export interface CryptoWatchDetailResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  message: string;
  item: CryptoWatchItem;
  leftOrderBook: CryptoOrderBook;
  rightOrderBook: CryptoOrderBook;
  history: CryptoWatchHistoryResponse;
  pushRule: CryptoPushRule;
  pushLogs: CryptoPushLog[];
}

export interface CryptoWatchPayload {
  symbol: string;
  leftExchange: CryptoExchange;
  rightExchange: CryptoExchange;
  leftMarketType?: CryptoMarketType;
  rightMarketType?: CryptoMarketType;
  enabled?: boolean;
  note?: string | null;
}

export interface CryptoWatchUpdatePayload {
  symbol?: string;
  leftExchange?: CryptoExchange;
  rightExchange?: CryptoExchange;
  leftMarketType?: CryptoMarketType;
  rightMarketType?: CryptoMarketType;
  enabled?: boolean;
  note?: string | null;
}

export interface CryptoRefreshResult {
  status: SourceStatus;
  message: string;
  refreshed: number;
  failed: number;
}

export type JudgmentConclusion = "通过" | "观察" | "降级" | "放弃";

export interface JudgmentMotherCard {
  id: string;
  title: string;
  problem: string;
  call_conditions: string[];
  checklist: string[];
  supporting_card_ids: string[];
  boundaries: string[];
  revision_notes: string[];
  reuse_hint: string;
}

export interface JudgmentAssistantCard {
  id: string;
  title: string;
  core: string;
  mother_card_ids: string[];
  checklist: string[];
  boundaries: string[];
  revision_notes: string[];
  reuse_hint: string;
}

export interface JudgmentCardRelation {
  mother_card_id: string;
  card_id: string;
}

export interface JudgmentRecord {
  id: string;
  title: string;
  date: string;
  judgment_object: string;
  called_cards: string[];
  satisfied: string;
  unsatisfied: string;
  conclusion: JudgmentConclusion | string;
  next_validation: string;
  note: string;
}

export interface JudgmentRecordPayload {
  judgment_object: string;
  called_cards: string[];
  satisfied?: string | null;
  unsatisfied?: string | null;
  conclusion: JudgmentConclusion;
  next_validation?: string | null;
  note?: string | null;
}

export interface JudgmentAssistantOverview {
  status: SourceStatus;
  message: string;
  library_path: string;
  updated_at: string | null;
  mother_cards: JudgmentMotherCard[];
  cards: JudgmentAssistantCard[];
  card_relations: JudgmentCardRelation[];
  recent_records: JudgmentRecord[];
}

export interface AStockSearch {
  code: string;
  name: string;
  exchange: string;
  full_code: string;
  pinyin: string;
  score: number;
}

export interface MarketReviewUniverseItem {
  id: number;
  market: string;
  symbol: string;
  name: string;
  theme: string;
  role: string;
  enabled: boolean;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface MarketReviewUniversePayload {
  market: string;
  symbol: string;
  name: string;
  theme?: string;
  role?: string;
  enabled?: boolean;
  sort_order?: number;
}

export interface MarketReviewMaterial {
  id: number;
  report_date: string;
  source_type: string;
  source_name: string;
  market: string;
  theme: string;
  title: string;
  content: string;
  url: string | null;
  status: SourceStatus;
  importance: number;
  created_at: string;
  updated_at: string;
}

export interface MarketReviewMaterialPayload {
  report_date?: string;
  source_name?: string;
  market?: string;
  theme?: string;
  title?: string;
  content?: string;
  url?: string | null;
  importance?: number;
}

export interface MarketReviewSourceStatus {
  source: string;
  status: SourceStatus;
  message: string;
  count: number;
}

export interface MarketReviewReport {
  id: number;
  period: "daily" | "weekly";
  report_date: string;
  start_date: string;
  end_date: string;
  title: string;
  status: SourceStatus;
  markdown_path: string;
  content: string;
  ai_status: SourceStatus;
  source_status: MarketReviewSourceStatus[];
  created_at: string;
  updated_at: string;
}

export interface MarketReviewAiSettings {
  provider: string;
  model: string;
  base_url: string;
  enabled: boolean;
  api_key_configured: boolean;
  api_key_masked: string | null;
  status: SourceStatus;
  effective_provider: string | null;
}

export interface MarketReviewAiSettingsPayload {
  provider?: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  enabled?: boolean;
  clear_api_key?: boolean;
}

export interface MarketReviewOverview {
  status: SourceStatus;
  source_status: SourceStatus;
  ai_status: SourceStatus;
  ai_settings: MarketReviewAiSettings;
  report_root: string;
  message: string;
  latest_report: MarketReviewReport | null;
  reports: MarketReviewReport[];
  materials: MarketReviewMaterial[];
  universe: MarketReviewUniverseItem[];
}

export interface MarketReviewActionResult {
  status: SourceStatus;
  message: string | null;
  report_date?: string | null;
  source_status: MarketReviewSourceStatus[];
  report: MarketReviewReport | null;
  matched_count: number;
  ignored_count: number;
}

export interface ExchangeAnnouncementSource {
  exchange: string;
  exchange_name: string;
  status: SourceStatus;
  count: number;
  raw_count?: number;
  expired_count?: number;
  message: string | null;
  last_success_at?: string | null;
  last_attempt_at?: string | null;
  age_seconds?: number | null;
  duration_ms?: number | null;
  refresh_interval_seconds?: number;
  next_refresh_seconds?: number;
  consecutive_failures?: number;
  cache_hit?: boolean;
  stale?: boolean;
}

export type ExchangeAnnouncementAssetType = "stock" | "crypto" | "unknown";

export interface ExchangeAnnouncementDetail {
  exchange: string;
  exchange_name: string;
  action:
    | "listing"
    | "delisting"
    | "suspension"
    | "resumption"
    | "migration"
    | "parameter_change"
    | "unknown";
  action_label: string;
  market_type:
    | "spot"
    | "contract"
    | "contract_usd"
    | "contract_usdc"
    | "margin_loan"
    | "unknown";
  market_label: string;
  asset_type: ExchangeAnnouncementAssetType;
  asset_label: string;
  title: string;
  url: string;
  published_at: string | null;
  event_at: string | null;
  has_occurred: boolean;
  seconds_until_event: number;
  event_status: "已发生" | "未发生";
  category: string;
  symbols: string[];
}

export interface ExchangeAnnouncement {
  key: string;
  push_key: string;
  symbol: string;
  title: string;
  exchange_names: string[];
  exchange_codes: string[];
  action_label: string;
  market_label: string;
  asset_type: ExchangeAnnouncementAssetType;
  asset_label: string;
  latest_published_at: string | null;
  event_at: string | null;
  has_occurred: boolean;
  seconds_until_event: number;
  event_status: "已发生" | "未发生";
  announcements: ExchangeAnnouncementDetail[];
  timeline?: ExchangeAnnouncementTimeline | null;
  astro_linkage?: ExchangeAnnouncementAstroLinkage | null;
}

export interface ExchangeAnnouncementTimeline {
  publishedAt: string | null;
  detectedAt: string | null;
  pushedAt: string | null;
  eventAt: string | null;
  marketOpenedAt: string | null;
  astroRegisteredAt: string | null;
  firstDirectCheckAt: string | null;
  lastDirectCheckAt: string | null;
  astroStatus: string;
  astroReason: string | null;
}

export interface ExchangeAnnouncementAstroLinkage {
  status: string;
  reason: string | null;
  registeredAt: string | null;
  firstDirectCheckAt: string | null;
  lastDirectCheckAt: string | null;
  routeCount: number;
  aboveThresholdRouteCount: number;
  cardCreatedAt?: string | null;
}

export interface ExchangeAnnouncementChange {
  id: number;
  announcementKey: string;
  symbol: string;
  changeType: "event_time_changed" | "content_changed" | "possibly_cancelled" | string;
  summary: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  createdAt: string | null;
}

export interface ExchangeAnnouncementPushLog {
  id: number;
  announcement_key: string;
  symbol: string;
  group_name: string;
  title: string;
  body: string;
  link: string | null;
  event_at: string | null;
  asset_type: ExchangeAnnouncementAssetType;
  asset_label: string;
  status: SourceStatus;
  message: string | null;
  created_at: string;
}

export interface ExchangeAnnouncementsResponse {
  status: SourceStatus;
  source_status: SourceStatus;
  push_status: SourceStatus;
  push_message: string;
  bark_status: SourceStatus;
  updated_at: string | null;
  items: ExchangeAnnouncement[];
  sources: ExchangeAnnouncementSource[];
  push_logs: ExchangeAnnouncementPushLog[];
  announcement_changes?: ExchangeAnnouncementChange[];
  opportunities?: ExchangeDelistingOpportunitiesResponse;
  message: string;
}

export interface ExchangeAnnouncementPushResult {
  status: SourceStatus;
  message: string;
  pushed_count: number;
  logged_count: number;
  ignored_count: number;
  bark_status: SourceStatus;
  push_status: SourceStatus;
  push_logs: ExchangeAnnouncementPushLog[];
}

export interface ExchangeAnnouncementPushClearResult {
  status: SourceStatus;
  message: string;
  cleared_count: number;
}

export interface ExchangeDelistingOpportunity {
  id: number;
  watchId: number;
  symbol: string;
  muted: boolean;
  eventAt: string | null;
  expiresAt: string;
  sourceExchange: string;
  sourceMarketType: "spot" | "futures";
  pairKey: string;
  pairType: "SF" | "FF" | "SS";
  leftExchange: string;
  leftExchangeName: string;
  leftMarketType: "spot" | "futures";
  rightExchange: string;
  rightExchangeName: string;
  rightMarketType: "spot" | "futures";
  leftBid: number | null;
  leftAsk: number | null;
  rightBid: number | null;
  rightAsk: number | null;
  referenceSpreadPct: number | null;
  sellLeftBuyRightPct: number | null;
  sellRightBuyLeftPct: number | null;
  bestExecutableSpreadPct: number | null;
  direction: string | null;
  status: SourceStatus | "signal_unverified" | "watch";
  lastError: string | null;
  updatedAt: string;
  astroUrl: string;
  executionGate: string;
}

export interface ExchangeDelistingOpportunitiesResponse {
  status: SourceStatus;
  updated_at: string | null;
  items: ExchangeDelistingOpportunity[];
  watch_count: number;
  muted_symbols: string[];
  message: string;
  formula: string;
  alert_threshold_pct: number;
  alert_cooldown_minutes: number;
  high_alert_threshold_pct: number;
  high_alert_cooldown_minutes: number;
  critical_alert_threshold_pct: number;
  critical_alert_cooldown_minutes: number;
  scan_interval_seconds: number;
}

export interface ExchangeDelistingOpportunityMuteResponse {
  status: SourceStatus;
  symbol: string;
  muted: boolean;
  message: string;
}

export interface ExchangeDelistingOpportunityDeleteResponse {
  status: SourceStatus;
  watch_id: number;
  pair_key: string;
  symbol: string;
  message: string;
}

export interface XueqiuTarget {
  id: string;
  targetDbId: number | null;
  nickname: string;
  avatarUrl: string;
  userId: string;
  profileUrl: string;
  portfolioUrl: string;
  enablePortfolio: boolean;
  enablePost: boolean;
  enablePortfolioPush: boolean;
  enablePostPush: boolean;
  createdAt: number;
  updatedAt: number;
  last_home_feed_status: SourceStatus;
  last_home_feed_message: string | null;
  last_home_feed_at: string | null;
  last_watchlist_status: SourceStatus;
  last_watchlist_message: string | null;
  last_watchlist_at: string | null;
}

export interface XueqiuPost {
  id: number;
  target_id: number;
  author_name: string;
  author_user_id: string | null;
  content: string;
  source_url: string | null;
  published_at: string | null;
  crawled_at: string;
}

export interface XueqiuEvent {
  id: number;
  target_id: number;
  target_nickname: string;
  event_type: "added" | "removed";
  stock_code: string;
  stock_name: string;
  exchange: string;
  full_code: string;
  price: number | null;
  source_url: string;
  push_status: SourceStatus;
  push_message: string | null;
  created_at: string;
}

export interface PushLog {
  id: number;
  target_id: number | null;
  group_name: string;
  event_type: string;
  title: string;
  body: string;
  link: string | null;
  status: SourceStatus;
  message: string | null;
  created_at: string;
}

export interface CrawlLog {
  id: number;
  scope: string;
  target_id: number | null;
  status: SourceStatus;
  message: string | null;
  matched_count: number;
  ignored_count: number;
  created_at: string;
}

export interface PortfolioStockStat {
  target_id: number;
  nickname: string;
  stock_code: string;
  stock_name: string;
  exchange: string;
  full_code: string;
  first_price: number | null;
  latest_price: number | null;
  profit_per_share: number | null;
  first_seen_at: string | null;
  latest_seen_at: string | null;
}

export interface PortfolioUserStat {
  target_id: number;
  nickname: string;
  success_count: number;
  total_count: number;
  success_rate: number | null;
  stocks: PortfolioStockStat[];
}

export interface PortfolioWinRateSummary {
  target_id: number;
  nickname: string;
  window_days: number;
  total_count: number;
  closed_count: number;
  open_count: number;
  closed_win_count: number;
  closed_loss_count: number;
  open_win_count: number;
  open_loss_count: number;
  closed_win_rate: number | null;
  open_win_rate: number | null;
  average_return_pct: number | null;
  profit_stock_names: string[];
  loss_stock_names: string[];
}

export interface PortfolioWinRateItem {
  target_id: number;
  nickname: string;
  stock_code: string;
  stock_name: string;
  full_code: string;
  start_at: string | null;
  start_price: number | null;
  end_at: string | null;
  end_price: number | null;
  status: "已结束" | "持有中";
  return_pct: number | null;
}

export interface PortfolioWinRateOverview {
  summaries: PortfolioWinRateSummary[];
  recent_items: PortfolioWinRateItem[];
}

export interface XueqiuRecommendationSummary {
  target_id: number;
  nickname: string;
  window_days: number;
  total_count: number;
  closed_count: number;
  open_count: number;
  closed_win_count: number;
  closed_loss_count: number;
  open_win_count: number;
  open_loss_count: number;
  closed_win_rate: number | null;
  open_win_rate: number | null;
  average_return_pct: number | null;
  profit_stock_names: string[];
  loss_stock_names: string[];
}

export interface XueqiuRecommendationItem {
  id: number;
  target_id: number;
  nickname: string;
  stock_code: string;
  stock_name: string;
  exchange: string;
  full_code: string;
  source_type: string;
  source_post_id: number | null;
  source_url: string | null;
  source_excerpt: string | null;
  start_at: string | null;
  start_price: number | null;
  end_at: string | null;
  end_price: number | null;
  status: "已结束" | "持有中";
  return_pct: number | null;
}

export interface XueqiuRecommendedStocksOverview {
  summaries: XueqiuRecommendationSummary[];
  recent_items: XueqiuRecommendationItem[];
}

export interface XueqiuOverview {
  status: SourceStatus;
  authorization_status: SourceStatus;
  bark_status: SourceStatus;
  profile_dir: string;
  targets: XueqiuTarget[];
  recent_posts: XueqiuPost[];
  recent_events: XueqiuEvent[];
  push_logs: PushLog[];
  crawl_logs: CrawlLog[];
  portfolio_stats: PortfolioUserStat[];
  portfolio_win_rate: PortfolioWinRateOverview;
  recommended_stocks: XueqiuRecommendedStocksOverview;
}

export interface XueqiuSummary {
  status: SourceStatus;
  authorization_status: SourceStatus;
  bark_status: SourceStatus;
  profile_dir: string;
  targets: XueqiuTarget[];
  post_counts: Record<string, number>;
}

export interface XueqiuActivity {
  recent_events: XueqiuEvent[];
  push_logs: PushLog[];
  crawl_logs: CrawlLog[];
}

export interface XueqiuPortfolioSection {
  portfolio_stats: PortfolioUserStat[];
  portfolio_win_rate: PortfolioWinRateOverview;
}

export interface XueqiuRecommendationSection {
  recommended_stocks: XueqiuRecommendedStocksOverview;
}

export interface NetworkMessageSource {
  source_type: string;
  source_name: string;
  status: SourceStatus;
  message: string | null;
  updated_at: string | null;
}

export interface NetworkMessageItem {
  id: string;
  source_type: "xueqiu" | "zsxq" | string;
  source_name: string;
  message_type: string;
  matched_stocks: string[];
  author: string | null;
  title: string;
  content: string;
  image_count: number;
  image_status: SourceStatus | null;
  image_text: string | null;
  image_message: string | null;
  link: string | null;
  status: SourceStatus;
  created_at: string;
}

export interface NetworkMessagesOverview {
  status: SourceStatus;
  source_status: SourceStatus;
  updated_at: string | null;
  message: string;
  sources: NetworkMessageSource[];
  items: NetworkMessageItem[];
}

export interface TargetPayload {
  profileUrl: string;
}

export interface NeedVerifyResponse {
  success: false;
  reason: "NEED_VERIFY";
}

export interface TargetUpdatePayload {
  enablePortfolio?: boolean;
  enablePost?: boolean;
  enablePortfolioPush?: boolean;
  enablePostPush?: boolean;
}

export interface CrawlResult {
  status: SourceStatus;
  message: string | null;
  matched_count: number;
  ignored_count: number;
  target_id?: number | null;
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, messageText = "请求超时"): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(messageText)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

export const api = {
  simple: (path: string) => request<SimplePageResponse>(path),
  researchRuns: () => request<StockResearchGenerationRun[]>("/api/research-runs"),
  researchRun: (id: number) => request<StockResearchGenerationRunDetail>(`/api/research-runs/${id}`),
  deleteResearchRun: (id: number, deleteRecords = false) =>
    request<{ status: string; message: string; deleted_run_id: number; deleted_records: number }>(
      `/api/research-runs/${id}?delete_records=${deleteRecords ? "true" : "false"}`,
      { method: "DELETE" }
    ),
  updateResearchRunItem: (id: number, payload: StockResearchGenerationItemUpdatePayload) =>
    request<StockResearchGenerationItem>(`/api/research-runs/items/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  applyResearchRunItemTags: (id: number, tagNames?: string[]) =>
    request<StockResearchGenerationItem>(`/api/research-runs/items/${id}/apply-tags`, {
      method: "POST",
      body: JSON.stringify({ tag_names: tagNames ?? null })
    }),
  applyResearchRunTags: (id: number) =>
    request<StockResearchActionResult>(`/api/research-runs/${id}/apply-tags`, {
      method: "POST"
    }),
  markResearchRunProcessed: (id: number) =>
    request<StockResearchActionResult>(`/api/research-runs/${id}/mark-processed`, {
      method: "POST"
    }),
  createQqResearchItems: (payload: StockResearchQqItemsPayload) =>
    request<StockResearchActionResult>("/api/research-runs/qq-items", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  seedInitialStockReviews: () =>
    request<StockResearchActionResult>("/api/stocks/batch-initial-review", {
      method: "POST"
    }),
  stockDetail: (fullCode: string) => request<StockDetail>(`/api/stocks/${encodeURIComponent(fullCode)}`),
  stockInformationFlow: (fullCode: string) =>
    request<StockInformationFlowResponse>(`/api/stocks/${encodeURIComponent(fullCode)}/information-flow`),
  createStockAnalysisRecord: (fullCode: string, payload: StockAnalysisRecordPayload) =>
    request<StockAnalysisRecord>(`/api/stocks/${encodeURIComponent(fullCode)}/analysis-records`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  factors: () => request<FactorOverview>("/api/factors"),
  factorsStatus: () => request<FactorStatusResponse>("/api/factors/status"),
  factorEffectBacktest: () => request<FactorEffectBacktest>("/api/factors/effect-backtest"),
  factorEffectLogs: () => request<FactorEffectDailyLogsResponse>("/api/factors/effect-logs"),
  generateFactorEffectLog: () =>
    request<FactorEffectDailyLog>("/api/factors/effect-logs", {
      method: "POST"
    }),
  factorTags: () => request<FactorTag[]>("/api/factors/tags"),
  factorTagStocks: (tagId: number, period = 3, direction: "winning" | "losing" = "winning") =>
    request<FactorTagStockDetail>(
      `/api/factors/tags/${tagId}/stocks?period=${encodeURIComponent(period)}&direction=${encodeURIComponent(direction)}`
    ),
  createFactorTag: (payload: FactorTagPayload) =>
    request<FactorTag>("/api/factors/tags", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  createFactorTagsBulk: (payload: FactorTagBulkPayload) =>
    request<FactorTag[]>("/api/factors/tags/bulk", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  factorTagCandidates: () => request<FactorTagCandidatesResponse>("/api/factors/tag-candidates"),
  runFactorTagCandidates: (payload: FactorTagCandidateRunPayload = {}) =>
    request<FactorTagCandidatesResponse>("/api/factors/tag-candidates/run", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  factorAutoTags: () => request<FactorAutoTagResponse>("/api/factors/auto-tags"),
  runFactorAutoTags: () =>
    request<FactorAutoTagResponse>("/api/factors/auto-tags/run", {
      method: "POST"
    }),
  factorTaggedStocks: (limit = 800, offset = 0, query = "") =>
    request<FactorTaggedStocksResponse>(
      `/api/factors/tagged-stocks?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}&query=${encodeURIComponent(query)}`
    ),
  factorHotTagReview: (gainLimit = 200, amountLimit = 200) =>
    request<FactorHotTagReviewResponse>(
      `/api/factors/hot-tag-review?gain_limit=${encodeURIComponent(gainLimit)}&amount_limit=${encodeURIComponent(amountLimit)}`
    ),
  updateFactorTag: (id: number, payload: FactorTagPayload) =>
    request<FactorTag>(`/api/factors/tags/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteFactorTag: (id: number) =>
    request<{ status: string; message: string; removed_stock_count: number; tag_name: string }>(`/api/factors/tags/${id}`, {
      method: "DELETE"
    }),
  factorStockTags: (fullCode: string) =>
    request<FactorStockTagResponse>(`/api/factors/stocks/${encodeURIComponent(fullCode)}/tags`),
  updateFactorStockTags: (fullCode: string, tagIds: number[]) =>
    request<FactorStockTagResponse>(`/api/factors/stocks/${encodeURIComponent(fullCode)}/tags`, {
      method: "PATCH",
      body: JSON.stringify({ tag_ids: tagIds })
    }),
  updateFactorStockStatus: (fullCode: string, status: FactorTagStatus, reviewReasons: string[] = []) =>
    request<FactorStockTagResponse>(`/api/factors/stocks/${encodeURIComponent(fullCode)}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status, review_reasons: reviewReasons })
    }),
  excludeFactorStock: (fullCode: string) =>
    request<{ status: string; message: string }>(`/api/factors/stocks/${encodeURIComponent(fullCode)}`, {
      method: "DELETE"
    }),
  factorPresets: () => request<FactorPreset[]>("/api/factors/presets"),
  createFactorPreset: (payload: FactorPresetPayload) =>
    request<FactorPreset>("/api/factors/presets", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateFactorPreset: (id: number, payload: FactorPresetPayload) =>
    request<FactorPreset>(`/api/factors/presets/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteFactorPreset: (id: number) =>
    request<{ status: string; message: string }>(`/api/factors/presets/${id}`, {
      method: "DELETE"
    }),
  activateFactorPreset: (id: number) =>
    request<FactorPreset>(`/api/factors/presets/${id}/activate`, {
      method: "POST"
    }),
  opportunityMap: () => withTimeout(request<OpportunityOverview>("/api/factors/opportunity-map"), 12_000, "产业机会矩阵加载超时"),
  createOpportunityGroup: (payload: OpportunityGroupPayload) =>
    request<OpportunityGroup>("/api/factors/opportunity-map/groups", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateOpportunityGroup: (id: number, payload: OpportunityGroupPayload) =>
    request<OpportunityGroup>(`/api/factors/opportunity-map/groups/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteOpportunityGroup: (id: number) =>
    request<{ status: string; message: string }>(`/api/factors/opportunity-map/groups/${id}`, {
      method: "DELETE"
    }),
  createOpportunityItem: (groupId: number, payload: OpportunityItemPayload) =>
    request<OpportunityItem>(`/api/factors/opportunity-map/groups/${groupId}/items`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateOpportunityItem: (id: number, payload: OpportunityItemPayload) =>
    request<OpportunityItem>(`/api/factors/opportunity-map/items/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteOpportunityItem: (id: number) =>
    request<{ status: string; message: string }>(`/api/factors/opportunity-map/items/${id}`, {
      method: "DELETE"
    }),
  industryChains: () => request<IndustryChainOverview>("/api/industry-chains"),
  industryChain: (id: number) => request<IndustryChainDetail>(`/api/industry-chains/${id}`),
  createIndustryChain: (payload: IndustryChainPayload) =>
    request<IndustryChainDetail>("/api/industry-chains", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateIndustryChain: (id: number, payload: IndustryChainPayload) =>
    request<IndustryChainDetail>(`/api/industry-chains/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteIndustryChain: (id: number) =>
    request<{ status: string; message: string }>(`/api/industry-chains/${id}`, {
      method: "DELETE"
    }),
  createIndustrySegment: (chainId: number, payload: IndustryChainSegmentPayload) =>
    request<IndustryChainSegment>(`/api/industry-chains/${chainId}/segments`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateIndustrySegment: (id: number, payload: IndustryChainSegmentPayload) =>
    request<IndustryChainSegment>(`/api/industry-chains/segments/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteIndustrySegment: (id: number) =>
    request<{ status: string; message: string }>(`/api/industry-chains/segments/${id}`, {
      method: "DELETE"
    }),
  createIndustryCompany: (chainId: number, payload: IndustryChainCompanyPayload) =>
    request<IndustryChainCompany>(`/api/industry-chains/${chainId}/companies`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateIndustryCompany: (id: number, payload: IndustryChainCompanyPayload) =>
    request<IndustryChainCompany>(`/api/industry-chains/companies/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteIndustryCompany: (id: number) =>
    request<{ status: string; message: string }>(`/api/industry-chains/companies/${id}`, {
      method: "DELETE"
    }),
  createIndustryEvidence: (chainId: number, payload: IndustryChainEvidencePayload) =>
    request<IndustryChainEvidence>(`/api/industry-chains/${chainId}/evidence`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateIndustryEvidence: (id: number, payload: IndustryChainEvidencePayload) =>
    request<IndustryChainEvidence>(`/api/industry-chains/evidence/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteIndustryEvidence: (id: number) =>
    request<{ status: string; message: string }>(`/api/industry-chains/evidence/${id}`, {
      method: "DELETE"
    }),
  createIndustryTask: (chainId: number, payload: IndustryChainTaskPayload) =>
    request<IndustryChainTask>(`/api/industry-chains/${chainId}/tasks`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateIndustryTask: (id: number, payload: IndustryChainTaskPayload) =>
    request<IndustryChainTask>(`/api/industry-chains/tasks/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteIndustryTask: (id: number) =>
    request<{ status: string; message: string }>(`/api/industry-chains/tasks/${id}`, {
      method: "DELETE"
    }),
  createIndustryOpportunityLink: (payload: IndustryChainOpportunityLinkPayload) =>
    request<{ status: string; message: string }>("/api/industry-chains/opportunity-links", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  deleteIndustryOpportunityLink: (id: number) =>
    request<{ status: string; message: string }>(`/api/industry-chains/opportunity-links/${id}`, {
      method: "DELETE"
    }),
  sectorIndices: () => request<SectorIndexOverview>("/api/sector-indices"),
  marketStyleCacheStatus: () => request<MarketStyleCacheStatus>("/api/market-style/cache/status"),
  refreshMarketStyleCache: () =>
    request<MarketStyleCacheRefreshResult>("/api/market-style/cache/refresh", {
      method: "POST"
    }),
  runMarketStyleBacktest: () =>
    request<MarketStyleBacktestResult>("/api/market-style/backtest/run", {
      method: "POST"
    }),
  createSectorIndex: (payload: SectorIndexPayload) =>
    request<SectorIndexSummary>("/api/sector-indices", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  sectorIndexDetail: (id: number) => request<SectorIndexDetail>(`/api/sector-indices/${id}`),
  updateSectorIndex: (id: number, payload: SectorIndexPayload) =>
    request<SectorIndexSummary>(`/api/sector-indices/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteSectorIndex: (id: number) =>
    request<{ status: string; message: string }>(`/api/sector-indices/${id}`, {
      method: "DELETE"
    }),
  sectorIndexBars: (id: number, range = "6m") =>
    request<SectorIndexBar[]>(`/api/sector-indices/${id}/bars?range=${encodeURIComponent(range)}`),
  stockBars: (fullCode: string, range = "6m", refresh = false) =>
    request<StockBar[]>(
      `/api/stocks/${encodeURIComponent(fullCode)}/bars?range=${encodeURIComponent(range)}${refresh ? "&refresh=1" : ""}`
    ),
  addSectorIndexMember: (id: number, query: string, source = "manual", sourceNote?: string | null) =>
    request<SectorIndexMember>(`/api/sector-indices/${id}/members`, {
      method: "POST",
      body: JSON.stringify({ query, source, source_note: sourceNote ?? null })
    }),
  deleteSectorIndexMember: (id: number, fullCode: string) =>
    request<{ status: string; message: string }>(
      `/api/sector-indices/${id}/members/${encodeURIComponent(fullCode)}`,
      {
        method: "DELETE"
      }
    ),
  recalculateSectorIndex: (id: number) =>
    request<SectorIndexRecalculateResult>(`/api/sector-indices/${id}/recalculate`, {
      method: "POST"
    }),
  importSectorIndexImage: (id: number, file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return request<SectorIndexImageImportResponse>(`/api/sector-indices/${id}/image-import`, {
      method: "POST",
      body: formData
    });
  },
  judgmentAssistant: () => request<JudgmentAssistantOverview>("/api/judgment-assistant"),
  createJudgmentRecord: (payload: JudgmentRecordPayload) =>
    request<JudgmentRecord>("/api/judgment-assistant/records", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  marketReview: () => request<MarketReviewOverview>("/api/market-review"),
  updateMarketReviewAiSettings: (payload: MarketReviewAiSettingsPayload) =>
    request<MarketReviewAiSettings>("/api/market-review/ai-settings", {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  createMarketReviewMaterial: (payload: MarketReviewMaterialPayload) =>
    request<MarketReviewMaterial>("/api/market-review/materials", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateMarketReviewMaterial: (id: number, payload: MarketReviewMaterialPayload) =>
    request<MarketReviewMaterial>(`/api/market-review/materials/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteMarketReviewMaterial: (id: number) =>
    request<{ status: string; message: string }>(`/api/market-review/materials/${id}`, {
      method: "DELETE"
    }),
  createMarketReviewUniverse: (payload: MarketReviewUniversePayload) =>
    request<MarketReviewUniverseItem>("/api/market-review/universe", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateMarketReviewUniverse: (id: number, payload: Partial<MarketReviewUniversePayload>) =>
    request<MarketReviewUniverseItem>(`/api/market-review/universe/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteMarketReviewUniverse: (id: number) =>
    request<{ status: string; message: string }>(`/api/market-review/universe/${id}`, {
      method: "DELETE"
    }),
  collectMarketReviewDaily: () =>
    request<MarketReviewActionResult>("/api/market-review/collect/daily", {
      method: "POST",
      body: JSON.stringify({})
    }),
  generateMarketReviewDaily: () =>
    request<MarketReviewActionResult>("/api/market-review/reports/daily", {
      method: "POST",
      body: JSON.stringify({})
    }),
  generateMarketReviewWeekly: () =>
    request<MarketReviewActionResult>("/api/market-review/reports/weekly", {
      method: "POST",
      body: JSON.stringify({})
    }),
  generateMarketReviewAi: (id: number) =>
    request<MarketReviewActionResult>(`/api/market-review/reports/${id}/ai`, {
      method: "POST"
    }),
  exchangeAnnouncements: (refresh = false) => request<ExchangeAnnouncementsResponse>(`/api/exchange-announcements${refresh ? "?refresh=1" : ""}`),
  pushExchangeAnnouncements: () =>
    request<ExchangeAnnouncementPushResult>("/api/exchange-announcements/push", {
      method: "POST"
    }),
  clearExchangeAnnouncementPushLogs: () =>
    request<ExchangeAnnouncementPushClearResult>("/api/exchange-announcements/push-logs", {
      method: "DELETE"
    }),
  updateExchangeDelistingOpportunityMute: (symbol: string, muted: boolean) =>
    request<ExchangeDelistingOpportunityMuteResponse>(
      `/api/exchange-announcements/opportunities/${encodeURIComponent(symbol)}/mute?muted=${muted ? "true" : "false"}`,
      { method: "PATCH" }
    ),
  deleteExchangeDelistingOpportunityPair: (watchId: number, pairKey: string) =>
    request<ExchangeDelistingOpportunityDeleteResponse>(
      `/api/exchange-announcements/opportunities/${watchId}/pairs/${encodeURIComponent(pairKey)}`,
      { method: "DELETE" }
    ),
  searchAStocks: (keyword: string) =>
    request<AStockSearch[]>(
      `/api/a-stocks/search?keyword=${encodeURIComponent(keyword)}`
    ),
  fsBorrowSearch: (symbol: string, refresh = false) =>
    request<CryptoFsBorrowSearchResponse>(
      `/api/fs/borrow-search?symbol=${encodeURIComponent(symbol)}${refresh ? "&refresh=true" : ""}`
    ),
  fsBorrowWatch: () => request<CryptoBorrowWatchResponse>("/api/fs/borrow-watch"),
  addFsBorrowWatch: (symbol: string) =>
    request<CryptoBorrowWatchResponse>(`/api/fs/borrow-watch?symbol=${encodeURIComponent(symbol)}`, {
      method: "POST"
    }),
  deleteFsBorrowWatch: (symbol: string) =>
    request<CryptoBorrowWatchResponse>(`/api/fs/borrow-watch?symbol=${encodeURIComponent(symbol)}`, {
      method: "DELETE"
    }),
  refreshFsBorrowWatch: () =>
    request<CryptoBorrowWatchResponse>("/api/fs/borrow-watch/refresh", {
      method: "POST"
    }),
  fsFundingCapWatch: () =>
    request<CryptoFundingCapWatchResponse>("/api/fs/funding-cap-watch"),
  addFsFundingCapWatch: (symbol: string, exchanges: CryptoExchange[]) =>
    request<CryptoFundingCapWatchResponse>(
      `/api/fs/funding-cap-watch?symbol=${encodeURIComponent(symbol)}&exchanges=${encodeURIComponent(exchanges.join(","))}`,
      { method: "POST" }
    ),
  updateFsFundingCapWatch: (symbol: string, exchanges: CryptoExchange[]) =>
    request<CryptoFundingCapWatchResponse>(
      `/api/fs/funding-cap-watch?symbol=${encodeURIComponent(symbol)}&exchanges=${encodeURIComponent(exchanges.join(","))}`,
      { method: "PATCH" }
    ),
  deleteFsFundingCapWatch: (symbol: string) =>
    request<CryptoFundingCapWatchResponse>(
      `/api/fs/funding-cap-watch?symbol=${encodeURIComponent(symbol)}`,
      { method: "DELETE" }
    ),
  refreshFsFundingCapWatch: () =>
    request<CryptoFundingCapWatchResponse>("/api/fs/funding-cap-watch/refresh", {
      method: "POST"
    }),
  fsSignals: (limit = 50) => request<CryptoFsSignalsResponse>(`/api/fs/signals?limit=${limit}`),
  fsObservationSummary: (days = 7) =>
    request<CryptoFsObservationSummary>(`/api/fs/observations/summary?days=${days}`),
  fsScheduler: () => request<CryptoFsSchedulerStatus>("/api/fs/scheduler"),
  startFsScheduler: () =>
    request<CryptoFsSchedulerStatus>("/api/fs/scheduler/start", {
      method: "POST"
    }),
  pauseFsScheduler: () =>
    request<CryptoFsSchedulerStatus>("/api/fs/scheduler/pause", {
      method: "POST"
    }),
  fsSettings: () => request<CryptoSettings>("/api/fs/settings"),
  scanFsSymbolMappings: () =>
    request<CryptoSymbolMappingScanResponse>("/api/fs/symbol-mappings/scan", {
      method: "POST"
    }),
  createFsSymbolMapping: (payload: CryptoSymbolMappingPayload) =>
    request<CryptoSymbolMapping>("/api/fs/symbol-mappings", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  updateFsSymbolMapping: (id: number, payload: CryptoSymbolMappingUpdatePayload) =>
    request<CryptoSymbolMapping>(`/api/fs/symbol-mappings/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),
  deleteFsSymbolMapping: (id: number) =>
    request<{ status: string; message: string }>(`/api/fs/symbol-mappings/${id}`, {
      method: "DELETE"
    }),
  networkMessages: (refresh = false) => request<NetworkMessagesOverview>(`/api/network-messages${refresh ? "?refresh=1" : ""}`),
  crawlNetworkZsxq: () =>
    request<CrawlResult>("/api/network-messages/crawl/zsxq", {
      method: "POST"
    })
};
