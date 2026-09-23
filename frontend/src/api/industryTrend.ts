import { request } from "../apiClient";
import type { DecisionPolicyEvaluation } from "./decisionReview";

export type IndustryPhase = "观察期" | "萌芽期" | "验证期" | "增长期" | "爆发期" | "成熟期" | "退潮期";
export type AttentionLevel = "重点跟踪" | "持续跟踪" | "观察" | "暂停";
export type InvestmentVerdict = "通过" | "观察" | "否决";
export type PricingStatus = "未定价" | "部分定价" | "充分定价";
export type ExpectationGapStatus = "正向预期差" | "基本匹配" | "负向预期差" | "无法判断";
export type NodeType = "需求驱动" | "网络与系统" | "光互联产品" | "核心器件" | "制造与配套";
export type NodeMaturityStatus = "前沿储备" | "验证中" | "小批量" | "放量中" | "成熟应用";
export type CompanyMarket = "A股" | "美股" | "台湾" | "韩国" | "日本" | "其他";
export type TrackingStatus = "核心受益" | "重点跟踪" | "观察" | "淘汰";
export type VerificationStatus = "未验证" | "验证中" | "已确认" | "失败";
export type CatalystStatus = "预期" | "确认" | "兑现";

export interface IndustryTrendSummary {
  id: number;
  name: string;
  summary: string | null;
  phase: IndustryPhase;
  strength: number;
  attention_level: AttentionLevel;
  direction_verdict: InvestmentVerdict;
  stock_verdict: InvestmentVerdict;
  timing_verdict: InvestmentVerdict;
  overall_verdict: InvestmentVerdict;
  pricing_status: PricingStatus;
  primary_company_id: number | null;
  primary_company: IndustryTrendCompany | null;
  company_recommendations: IndustryCompanyRecommendations;
  phase_entered_at: string | null;
  last_change_at: string | null;
  revision: number;
  status: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
  validation_summary: { total: number; confirmed: number; in_progress: number; failed: number };
  next_catalyst: IndustryTrendCatalyst | null;
  pending_material_count: number;
  market_snapshot: IndustryMarketSnapshot | null;
  decision_policy: DecisionPolicyEvaluation;
}

export interface IndustryMarketSnapshot {
  trade_date: string;
  close: number;
  change_pct: number | null;
  amount: number | null;
  return_5d_pct: number | null;
  return_20d_pct: number | null;
  return_60d_pct: number | null;
  amount_ratio_5d: number | null;
  distance_from_20d_high_pct: number | null;
  bars_available: number;
}

export interface IndustryDynamicsSnapshot {
  as_of: string;
  action: "可进入交易计划" | "等待触发" | "只跟踪，不追" | "风险收缩" | "信息不足";
  action_tone: "positive" | "watch" | "negative" | "neutral";
  action_reason: string;
  evidence: {
    status: string;
    count: number;
    confirmed: number;
    supporting: number;
    hard_facts: number;
    verified_information: number;
    partial_information: number;
    items: IndustryDynamicsDetailItem[];
  };
  attention: {
    status: string;
    coverage: "可用" | "待建立" | "不足";
    sample_7d: number;
    sample_previous_7d: number;
    source_count_7d: number;
    channel_count_7d: number;
    baseline_complete: boolean;
    xq_7d: number;
    xq_previous_7d: number;
    xq_author_count_7d: number;
    information_7d: number;
    information_previous_7d: number;
    information_14d: number;
    items: IndustryDynamicsDetailItem[];
  };
  market: {
    status: string;
    company_count: number;
    breadth_1d_pct: number | null;
    breadth_5d_pct: number | null;
    breadth_20d_pct: number | null;
    amount_expansion_pct: number | null;
    average_return_20d_pct: number | null;
    sample_adequate: boolean;
    is_stale: boolean;
    primary_confirmed: boolean;
  };
  expression: {
    status: string;
    company: string | null;
    verified: boolean;
    structural_ready: boolean;
    market_confirmed: boolean;
    return_5d_pct: number | null;
    amount_ratio_5d: number | null;
  };
  selling: { level: SellPressure; reason: string };
  decision_trace: {
    rule: string;
    conditions: Array<{ key: string; label: string; state: "pass" | "watch" | "danger"; summary: string }>;
    passed: string[];
    waiting: string[];
    risks: string[];
  };
  fresh_items: Array<{
    id: number;
    title: string;
    published_at: string | null;
    source_name: string;
    source_url: string | null;
    verification_status: string;
    price_status: string;
  }>;
  freshness: { market: string | null; xueqiu: string | null; information: string | null };
}

export interface IndustryDynamicsDetailItem {
  id: string;
  category: string;
  title: string;
  summary: string;
  source_name: string;
  source_url: string | null;
  published_at: string | null;
  status: string;
}

export interface IndustryTrendNode {
  id: number;
  chain_id: number;
  name: string;
  node_type: NodeType;
  plain_explanation: string | null;
  value_flow: string | null;
  watch_signal: string | null;
  maturity_status: NodeMaturityStatus | null;
  market_space: string | null;
  tech_barrier: string | null;
  competition: string | null;
  profit_elasticity: string | null;
  localization: string | null;
  investment_importance: string | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface IndustryTrendEdge {
  id: number;
  chain_id: number;
  from_node_id: number;
  to_node_id: number;
}

export interface IndustryTrendCompany {
  id: number;
  chain_id: number;
  code: string | null;
  name: string;
  exchange: string | null;
  full_code: string | null;
  market: CompanyMarket;
  external_url: string | null;
  position: string | null;
  company_standing: string | null;
  core_advantage: string | null;
  benefit_directness: string | null;
  profit_path: string | null;
  verification_status: VerificationStatus;
  tracking_status: TrackingStatus;
  pricing_status: PricingStatus;
  is_global_leader: boolean;
  is_domestic_alternative: boolean;
  is_primary: boolean;
  primary_reason: string | null;
  node_ids: number[];
  main_risk: string | null;
  market_implied_expectation: string | null;
  evidence_based_expectation: string | null;
  expectation_gap_status: ExpectationGapStatus;
  expectation_gap_reason: string | null;
  expectation_trigger: string | null;
  expectation_invalidation: string | null;
  expectation_as_of: string | null;
  expectation_anchor_market_cap: number | null;
  expectation_evidence_growth_pct: number | null;
  expectation_evidence_acceleration_pct: number | null;
  sort_order: number;
  latest_price: number | null;
  change_pct: number | null;
  quote_date: string | null;
  market_snapshot: IndustryMarketSnapshot | null;
  market_cap_snapshot: {
    total_market_cap: number;
    float_market_cap: number | null;
    as_of: string | null;
    source: string | null;
    method: string;
    is_estimated: boolean;
    base_market_cap: number;
    base_date: string | null;
  } | null;
  created_at: string;
  updated_at: string;
}

export interface CompanyExpectationPeriod {
  period: string;
  end_date: string;
  revenue: {
    average: number | null;
    low: number | null;
    high: number | null;
    analyst_count: number;
    year_ago: number | null;
    growth_pct: number | null;
  };
  eps: {
    average: number | null;
    low: number | null;
    high: number | null;
    analyst_count: number;
    year_ago: number | null;
    growth_pct: number | null;
  };
  eps_revision: {
    current: number | null;
    "7_days_ago": number | null;
    "30_days_ago": number | null;
    "60_days_ago": number | null;
    "90_days_ago": number | null;
    change_30d_pct: number | null;
    change_90d_pct: number | null;
    up_30d: number;
    down_30d: number;
  };
}

export interface CompanyExpectationAnalysis {
  id: number;
  company_id: number;
  symbol: string;
  market: string;
  provider: string;
  snapshot_date: string;
  status: string;
  message: string | null;
  stale: boolean;
  consensus: {
    symbol: string;
    currency: string;
    current_year: CompanyExpectationPeriod | null;
    next_year: CompanyExpectationPeriod | null;
    analyst_target: { average: number | null; median: number | null; low: number | null; high: number | null; analyst_count: number };
    source_note: string;
  };
  valuation: {
    as_of: string;
    current_price: number | null;
    post_market_price: number | null;
    market_cap: number | null;
    shares_outstanding: number | null;
    quote_source: string;
    forward_ps: number | null;
    forward_pe: number | null;
    consensus_net_income: number | null;
    consensus_net_margin_pct: number | null;
    pricing_state: string;
    conclusion: string;
    market_cap_bridge?: {
      forecast_period: string | null;
      currency: string | null;
      revenue: number | null;
      net_margin_pct: number | null;
      net_income: number | null;
      eps: number | null;
      forward_pe: number | null;
      forward_ps: number | null;
      market_cap: number | null;
      market_cap_from_profit: number | null;
      market_cap_from_revenue: number | null;
      formula: string;
      note: string;
    };
    support_drivers?: Array<{
      key: string;
      group: string;
      name: string;
      formula_role: string;
      status: string;
      value: number | null;
      value_kind: "currency" | "eps" | "percent" | "multiple";
      currency: string | null;
      change_pct: number | null;
      coverage_count: number;
      support_condition: string;
      invalidation: string;
      source: string;
    }>;
    reverse_check: {
      revenue_required_at_historical_median_ps: number | null;
      revenue_required_at_historical_p75_ps: number | null;
      eps_required_at_historical_median_pe: number | null;
      consensus_revenue_gap_vs_median_ps_pct: number | null;
      consensus_eps_gap_vs_median_pe_pct: number | null;
      interpretation: string;
    };
  };
  backtest: {
    method: string;
    sample_start: number | null;
    sample_end: number | null;
    forward_ps: { count: number; min: number | null; p25: number | null; median: number | null; p75: number | null; max: number | null };
    profitable_forward_pe: { count: number; min: number | null; p25: number | null; median: number | null; p75: number | null; max: number | null };
    rows: Array<{
      fiscal_year: number;
      filing_date: string;
      market_cap: number;
      next_year_revenue: number;
      next_year_net_income: number;
      forward_ps: number | null;
      forward_pe: number | null;
      next_252d_return_pct: number | null;
    }>;
  };
  sources: Array<{ name: string; url: string; tier: string; note: string }>;
}

export interface IndustryExpectationGroup {
  key: "all" | "global" | "domestic" | "other";
  label: string;
  company_count: number;
  consensus_count: number;
  paired_growth_count: number;
  positive_growth_count: number;
  negative_growth_count: number;
  positive_growth_ratio: number | null;
  median_revenue_growth_pct: number | null;
  median_eps_growth_pct: number | null;
  revision_sample_count: number;
  revision_up_count: number;
  revision_down_count: number;
  revision_up_ratio: number | null;
  median_eps_revision_30d_pct: number | null;
  median_forward_ps: number | null;
  median_forward_pe: number | null;
}

export interface ExpectationGapCalibrationMarket {
  state: "validated" | "not_validated" | "missing" | string;
  interpretation: string;
  thresholds: {
    evidence_growth_pct?: number;
    evidence_acceleration_pct?: number;
    unpriced_20d_excess_pct?: number;
    priced_20d_excess_pct?: number;
    priced_60d_excess_pct?: number;
    confirmation_excess_pct?: number;
  };
  validation: {
    horizon: number | null;
    a_sample_count: number | null;
    a_win_rate_pct: number | null;
    a_median_excess_return_pct: number | null;
    a_ci_low_pct: number | null;
    a_ci_high_pct: number | null;
    pricing_gain_a_vs_b_median_pct: number | null;
    filter_gain_a_vs_bc_median_pct: number | null;
  };
  usage: string;
}

export interface ExpectationGapCalibration {
  status: string;
  method_version: string | null;
  generated_at?: string | null;
  period: { start?: string; end?: string; years?: number };
  validation_start?: string | null;
  overall_state: string;
  overall_interpretation: string;
  markets: Record<string, ExpectationGapCalibrationMarket>;
  decision_rules: Record<"A" | "B" | "C" | "D", string> | Record<string, string>;
  guardrails: string[];
}

export interface CompanyExpectationGapGate {
  decision_code: "A" | "B" | "C" | "D" | "观察";
  label: string;
  reason: string;
  method_state: string;
  signal_date: string | null;
  formal_gap_status: string;
  does_not_overwrite_formal_conclusion: boolean;
  evidence: {
    state: string;
    growth_pct: number | null;
    acceleration_pct: number | null;
    hard_source_count: number;
    source_titles: string[];
    company_verified: boolean;
    threshold_growth_pct: number | null;
    threshold_acceleration_pct: number | null;
  };
  pricing: {
    state: string;
    pre_20d_excess_pct: number | null;
    pre_60d_excess_pct: number | null;
    unpriced_threshold_pct: number | null;
  };
  confirmation: {
    state: string;
    observation_date: string | null;
    excess_pct: number | null;
    threshold_pct: number | null;
  };
  execution: {
    state: string;
    entry_date: string | null;
    deferred_sessions?: number | null;
  };
}

export interface IndustryExpectationCompany {
  company_id: number;
  name: string;
  market: string;
  exchange: string | null;
  code: string | null;
  full_code: string | null;
  is_listed: boolean;
  is_supported: boolean;
  is_global_leader: boolean;
  is_domestic_alternative: boolean;
  is_primary: boolean;
  coverage_status: "已覆盖" | "无分析师覆盖" | "待抓取" | "未上市" | "市场待接入";
  provider: string | null;
  snapshot_date: string | null;
  forecast_period: string | null;
  revenue_analyst_count: number;
  eps_analyst_count: number;
  next_revenue_growth_pct: number | null;
  next_eps_growth_pct: number | null;
  eps_revision_30d_pct: number | null;
  forward_ps: number | null;
  forward_pe: number | null;
  pricing_state: string | null;
  market_cap: number | null;
  currency: string | null;
  expectation_gap_gate: CompanyExpectationGapGate | null;
}

export interface IndustryExpectationSummary {
  id: number;
  chain_id: number;
  chain_name: string;
  snapshot_date: string;
  universe: {
    company_rows: number;
    unique_stocks: number;
    listed_companies: number;
    unlisted_companies: number;
    supported_companies: number;
    consensus_companies: number;
    coverage_rate: number | null;
    no_analyst_coverage: number;
    pending_fetch: number;
  };
  groups: IndustryExpectationGroup[];
  consistency: {
    growth_status: string;
    revision_status: string;
    cross_group_status: string;
    conclusion: string;
  };
  expectation_gap_calibration: ExpectationGapCalibration;
  companies: IndustryExpectationCompany[];
  refresh: {
    mode?: string;
    attempted?: number;
    succeeded?: number;
    no_consensus?: number;
    skipped_today?: number;
    unsupported?: number;
    failed?: number;
    errors?: Array<{ company_id: number; name: string; message: string }>;
  };
  method_note: string;
  history: Array<{ snapshot_date: string; coverage_rate: number | null; growth_status: string; revision_status: string }>;
}

export interface IndustryCompanyRecommendation {
  id: number;
  name: string;
  market: CompanyMarket;
  full_code: string | null;
  position: string | null;
  reason: string | null;
  verification_status: VerificationStatus;
  pricing_status: PricingStatus;
}

export interface IndustryCompanyRecommendations {
  global_leaders: IndustryCompanyRecommendation[];
  domestic_alternatives: IndustryCompanyRecommendation[];
}

export interface IndustryTrendValidation {
  id: number;
  chain_id: number;
  company_id: number | null;
  node_id: number | null;
  name: string;
  criteria: string | null;
  current_result: string | null;
  status: VerificationStatus;
  target_date: string | null;
  source_name: string | null;
  source_url: string | null;
  priority: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface IndustryTrendCatalyst {
  id: number;
  chain_id: number;
  event_name: string;
  expected_time: string | null;
  event_type: string;
  impact_node_id: number | null;
  impact_company_id: number | null;
  importance: "高" | "中" | "低";
  status: CatalystStatus;
  impact: string | null;
  source_name: string | null;
  source_url: string | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface IndustryTrendUpdate {
  id: number;
  chain_id: number;
  update_date: string;
  content: string;
  source_name: string | null;
  source_url: string | null;
  impact: string | null;
  next_verification: string | null;
  affects_phase: boolean;
  affects_decision: boolean;
  phase_suggestion: string | null;
  decision_suggestion: Record<string, string>;
  causal_stage: CausalStage | null;
  evidence_type: CausalEvidenceType | null;
  signal_status: CausalSignalStatus | null;
  buyer_group: string | null;
  market_response: string | null;
  sell_pressure: SellPressure | null;
  counter_evidence: string | null;
  created_at: string;
  updated_at: string;
}

export type CausalStage = "真实变化" | "认知扩散" | "资金进入" | "筹码交换" | "拥挤退潮";
export type CausalEvidenceType = "硬事实" | "市场线索" | "盘面确认" | "市场推断";
export type CausalSignalStatus = "线索" | "已确认" | "减弱" | "失效";
export type SellPressure = "低" | "中" | "高";

export interface IndustryTrendSource {
  id: number;
  company_id: number | null;
  node_ids: number[];
  title: string;
  content: string | null;
  source_name: string | null;
  source_url: string | null;
  impact_level: string;
  source_tier: "官方硬证据" | "公司披露" | "行业标准" | "市场数据" | "雪球线索" | "Codex判断";
  verification_status: "已互证" | "单一来源" | "市场线索" | "待验证";
  evidence_state: "有效" | "待复核" | "已失效" | "存在冲突";
  valid_until: string | null;
  conflict_note: string | null;
  evidence_date: string;
}

export interface IndustryTrendMaterial {
  id: number;
  chain_id: number;
  title: string;
  content: string | null;
  source_type: string;
  source_name: string | null;
  source_url: string | null;
  material_date: string;
  change_type: "新增证据" | "信息冲突" | "证据失效" | "待验证";
  status: "待处理" | "已纳入" | "忽略";
  note: string | null;
  processed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface IndustryTrendDraft {
  id: number;
  draft_type: "initial" | "update" | "chat";
  source: string;
  payload: Record<string, unknown>;
  updated_at: string;
}

export interface IndustryTrendDetail extends IndustryTrendSummary {
  catalyst: string | null;
  risk: string | null;
  investment_logic: string | null;
  change_summary: string | null;
  why_now: string | null;
  drivers: string[];
  expected_duration: string | null;
  priced_in: string | null;
  not_priced_in: string | null;
  next_signal: string | null;
  invalidation: string | null;
  nodes: IndustryTrendNode[];
  edges: IndustryTrendEdge[];
  companies: IndustryTrendCompany[];
  catalysts: IndustryTrendCatalyst[];
  validations: IndustryTrendValidation[];
  updates: IndustryTrendUpdate[];
  sources: IndustryTrendSource[];
  materials: IndustryTrendMaterial[];
  dynamics: IndustryDynamicsSnapshot;
  draft: IndustryTrendDraft | null;
}

export interface IndustryIntelligenceEvidence {
  key?: string;
  signal_type?: "反向证据" | "验证失败" | "证伪条件";
  association_reason?: "共同需求";
  title: string;
  content: string | null;
  source_name?: string | null;
  source_url?: string | null;
  source_tier?: IndustryTrendSource["source_tier"];
  verification_status?: IndustryTrendSource["verification_status"];
  evidence_state?: IndustryTrendSource["evidence_state"];
  evidence_date?: string;
  impact_level?: string;
  affected_industries: Array<{ id: number; name: string }>;
}

export interface IndustryIntelligenceSector {
  id: number;
  name: string;
  phase: IndustryPhase;
  attention_level: AttentionLevel;
  direction_verdict: InvestmentVerdict;
  stock_verdict: InvestmentVerdict;
  timing_verdict: InvestmentVerdict;
  pricing_status: PricingStatus;
  primary_company: string | null;
  company_recommendations: IndustryCompanyRecommendations;
  sector_expectation: {
    status: string;
    listed_count: number;
    consensus_count: number;
    coverage_rate: number | null;
    growth_status: string;
    revision_status: string;
    summary: string;
    as_of: string | null;
  };
  validation_summary: { total: number; confirmed: number; in_progress: number; failed: number };
  evidence_summary: { total: number; hard: number; mutual: number; risk: number };
  next_catalyst: IndustryTrendCatalyst | null;
  latest_change: IndustryTrendUpdate | null;
  next_signal: string | null;
  invalidation: string | null;
  action: IndustryDynamicsSnapshot["action"];
  action_tone: IndustryDynamicsSnapshot["action_tone"];
  action_reason: string;
  evidence_status: string;
  attention: IndustryDynamicsSnapshot["attention"];
  market: IndustryDynamicsSnapshot["market"];
  expression: IndustryDynamicsSnapshot["expression"];
  freshness: IndustryDynamicsSnapshot["freshness"];
}

export interface IndustryIntelligenceFreshChange {
  id: number;
  item_key: string;
  title: string;
  summary: string;
  published_at: string | null;
  source_name: string;
  source_url: string | null;
  bucket: string;
  importance: number;
  verification_status: string;
  official_check_status: string;
  price_status: string;
  is_shared: boolean;
  affected_industries: Array<{
    id: number;
    name: string;
    match_score: number;
    match_method: "explicit" | "structured" | "automatic";
  }>;
  affected_nodes: Array<{ id: number; name: string; node_type: NodeType; industry_id: number }>;
  affected_companies: Array<{ id: number; name: string; code: string | null; market: CompanyMarket; industry_id: number }>;
  match_reasons: string[];
  auto_tier: "硬证据" | "部分验证" | "待验证线索";
  auto_action: string;
}

export interface IndustryIntelligence {
  theme: string;
  overall: {
    action: "出现可交易细分" | "等待触发" | "风险收缩" | "信息不足";
    tone: "positive" | "watch" | "negative" | "neutral";
    reason: string;
    latest_at: string | null;
    action_counts: Record<string, number>;
  };
  summary: {
    industries: number;
    sources: number;
    supporting_evidence: number;
    shared_evidence: number;
    risk_evidence: number;
    confirmed_validations: number;
    total_validations: number;
    fresh_changes_7d: number;
    verified_changes_7d: number;
    shared_changes_7d: number;
  };
  sectors: IndustryIntelligenceSector[];
  fresh_changes: IndustryIntelligenceFreshChange[];
  latest_changes: IndustryIntelligenceEvidence[];
  validation_route: Array<{
    stage: "需求" | "订单" | "价格" | "利润";
    total: number;
    confirmed: number;
    in_progress: number;
    failed: number;
    industries: Array<{ id: number; name: string }>;
  }>;
  supporting_evidence: IndustryIntelligenceEvidence[];
  risk_signals: IndustryIntelligenceEvidence[];
}

export interface IndustryCrossMarketBasket {
  status: "增强" | "转弱" | "分化" | "数据不足";
  return_1d_pct: number | null;
  return_5d_pct: number | null;
  return_20d_pct: number | null;
  breadth_5d_pct: number | null;
  sample_count: number;
  trade_date: string | null;
}

export interface IndustryCrossMarketProxy {
  company_id?: number | null;
  name: string;
  symbol: string;
  market: string;
  role: "需求温度" | "网络传导" | "直接产业链" | "A股表达";
  node_ids: number[];
  available: boolean;
  trade_date: string | null;
  return_1d_pct: number | null;
  return_5d_pct: number | null;
  return_20d_pct: number | null;
}

export interface IndustryCrossMarketSector {
  id: number;
  name: string;
  global_demand: IndustryCrossMarketBasket;
  overseas_direct: IndustryCrossMarketBasket;
  a_share: IndustryCrossMarketBasket;
  correlation_60d: number | null;
  relation_state: "有效" | "减弱" | "失效" | "数据不足";
  beta_60d: number | null;
  residual_20d_pct: number | null;
  divergence_z: number | null;
  divergence_state: "A股落后" | "A股领先" | "基本同步" | "数据不足";
  aligned_samples: number;
  action: "跨市场共振" | "偏离观察" | "风险收缩" | "检查独立催化" | "继续观察";
  action_tone: "positive" | "watch" | "negative" | "neutral";
  action_reason: string;
  proxies: {
    demand: IndustryCrossMarketProxy[];
    network: IndustryCrossMarketProxy[];
    direct: IndustryCrossMarketProxy[];
    a_share: IndustryCrossMarketProxy[];
  };
  node_mappings: Array<{
    node_id: number;
    node_name: string;
    node_type: NodeType;
    overseas: IndustryCrossMarketProxy[];
    a_share: IndustryCrossMarketProxy[];
  }>;
}

export interface IndustryCrossMarketIntelligence {
  as_of: string | null;
  source_status: "ok" | "partial" | "unavailable" | "empty";
  principle: string;
  global_demand: IndustryCrossMarketBasket;
  sectors: IndustryCrossMarketSector[];
  errors: string[];
  method: {
    alignment: string;
    windows: number[];
    source: string;
    cache_minutes: number;
  };
}

export interface IndustryTrendJob {
  id: number;
  chain_id: number;
  job_type: "initial" | "update";
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  phase: string;
  error_message: string | null;
  update_mode: "全面更新" | "首次研究";
  input_token_estimate: number;
  output_token_estimate: number;
  total_token_estimate: number;
  change_count: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  result: Record<string, unknown> | null;
}

export interface IndustryTrendResearchSetting {
  id: number | null;
  chain_id: number;
  update_mode: "全面更新";
  priority_nodes: string[];
  priority_companies: string[];
  source_preferences: string[];
  excluded_keywords: string[];
  evidence_rules: string | null;
  custom_instructions: string | null;
  token_budget: number;
  allow_new_nodes: boolean;
  allow_new_companies: boolean;
  draft_only: true;
  last_researched_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface TrendCorePayload {
  name?: string;
  summary?: string | null;
  phase?: IndustryPhase;
  strength?: number;
  catalyst?: string | null;
  risk?: string | null;
  investment_logic?: string | null;
  change_summary?: string | null;
  why_now?: string | null;
  drivers?: string[];
  expected_duration?: string | null;
  attention_level?: AttentionLevel;
  direction_verdict?: InvestmentVerdict;
  stock_verdict?: InvestmentVerdict;
  timing_verdict?: InvestmentVerdict;
  pricing_status?: PricingStatus;
  priced_in?: string | null;
  not_priced_in?: string | null;
  next_signal?: string | null;
  invalidation?: string | null;
  sort_order?: number;
}

export const industryTrendApi = {
  list: (query = "", phase = "", attention = "") => {
    const params = new URLSearchParams();
    if (query) params.set("query", query);
    if (phase) params.set("phase", phase);
    if (attention) params.set("attention_level", attention);
    const suffix = params.toString() ? `?${params}` : "";
    return request<{ items: IndustryTrendSummary[]; phases: IndustryPhase[]; attention_levels: AttentionLevel[] }>(`/api/investment/industry-trends${suffix}`);
  },
  intelligence: () => request<IndustryIntelligence>("/api/investment/industry-trends/intelligence"),
  crossMarketIntelligence: (refresh = false) => request<IndustryCrossMarketIntelligence>(`/api/investment/industry-trends/cross-market-intelligence${refresh ? "?refresh=true" : ""}`),
  get: (id: number) => request<IndustryTrendDetail>(`/api/investment/industry-trends/${id}`),
  create: (payload: { name: string; summary?: string; attention_level?: AttentionLevel }) => request<IndustryTrendDetail>("/api/investment/industry-trends", { method: "POST", body: JSON.stringify(payload) }),
  update: (id: number, payload: TrendCorePayload) => request<IndustryTrendDetail>(`/api/investment/industry-trends/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  createNode: (id: number, payload: Record<string, unknown>) => request<IndustryTrendNode>(`/api/investment/industry-trends/${id}/nodes`, { method: "POST", body: JSON.stringify(payload) }),
  updateNode: (id: number, nodeId: number, payload: Record<string, unknown>) => request<IndustryTrendNode>(`/api/investment/industry-trends/${id}/nodes/${nodeId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteNode: (id: number, nodeId: number) => request(`/api/investment/industry-trends/${id}/nodes/${nodeId}`, { method: "DELETE" }),
  createEdge: (id: number, payload: { from_node_id: number; to_node_id: number }) => request<IndustryTrendEdge>(`/api/investment/industry-trends/${id}/edges`, { method: "POST", body: JSON.stringify(payload) }),
  deleteEdge: (id: number, edgeId: number) => request(`/api/investment/industry-trends/${id}/edges/${edgeId}`, { method: "DELETE" }),
  createCompany: (id: number, payload: Record<string, unknown>) => request<IndustryTrendCompany>(`/api/investment/industry-trends/${id}/companies`, { method: "POST", body: JSON.stringify(payload) }),
  updateCompany: (id: number, companyId: number, payload: Record<string, unknown>) => request<IndustryTrendCompany>(`/api/investment/industry-trends/${id}/companies/${companyId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteCompany: (id: number, companyId: number) => request(`/api/investment/industry-trends/${id}/companies/${companyId}`, { method: "DELETE" }),
  companyExpectation: (id: number, companyId: number, refresh = false) => request<CompanyExpectationAnalysis>(`/api/investment/industry-trends/${id}/companies/${companyId}/expectation-analysis?view=valuation-drivers-v1${refresh ? "&refresh=true" : ""}`),
  refreshCompanyExpectation: (id: number, companyId: number) => request<CompanyExpectationAnalysis>(`/api/investment/industry-trends/${id}/companies/${companyId}/expectation-analysis/refresh`, { method: "POST" }),
  expectationSummary: (id: number) => request<IndustryExpectationSummary>(`/api/investment/industry-trends/${id}/expectation-summary`),
  refreshExpectationSummary: (id: number, mode: "missing" | "all" = "missing") => request<IndustryExpectationSummary>(`/api/investment/industry-trends/${id}/expectation-summary/refresh?mode=${mode}`, { method: "POST" }),
  createCatalyst: (id: number, payload: Record<string, unknown>) => request<IndustryTrendCatalyst>(`/api/investment/industry-trends/${id}/catalysts`, { method: "POST", body: JSON.stringify(payload) }),
  updateCatalyst: (id: number, catalystId: number, payload: Record<string, unknown>) => request<IndustryTrendCatalyst>(`/api/investment/industry-trends/${id}/catalysts/${catalystId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteCatalyst: (id: number, catalystId: number) => request(`/api/investment/industry-trends/${id}/catalysts/${catalystId}`, { method: "DELETE" }),
  createValidation: (id: number, payload: Record<string, unknown>) => request<IndustryTrendValidation>(`/api/investment/industry-trends/${id}/validations`, { method: "POST", body: JSON.stringify(payload) }),
  updateValidation: (id: number, validationId: number, payload: Record<string, unknown>) => request<IndustryTrendValidation>(`/api/investment/industry-trends/${id}/validations/${validationId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteValidation: (id: number, validationId: number) => request(`/api/investment/industry-trends/${id}/validations/${validationId}`, { method: "DELETE" }),
  createUpdate: (id: number, payload: Record<string, unknown>) => request<IndustryTrendUpdate>(`/api/investment/industry-trends/${id}/updates`, { method: "POST", body: JSON.stringify(payload) }),
  updateUpdate: (id: number, updateId: number, payload: Record<string, unknown>) => request<IndustryTrendUpdate>(`/api/investment/industry-trends/${id}/updates/${updateId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteUpdate: (id: number, updateId: number) => request(`/api/investment/industry-trends/${id}/updates/${updateId}`, { method: "DELETE" }),
  createMaterial: (id: number, payload: Record<string, unknown>) => request<IndustryTrendMaterial>(`/api/investment/industry-trends/${id}/materials`, { method: "POST", body: JSON.stringify(payload) }),
  updateMaterial: (id: number, materialId: number, payload: Record<string, unknown>) => request<IndustryTrendMaterial>(`/api/investment/industry-trends/${id}/materials/${materialId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteMaterial: (id: number, materialId: number) => request(`/api/investment/industry-trends/${id}/materials/${materialId}`, { method: "DELETE" }),
  updateEvidenceState: (id: number, evidenceId: number, payload: Record<string, unknown>) => request<IndustryTrendSource>(`/api/investment/industry-trends/${id}/sources/${evidenceId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  createDecision: (id: number, payload: Record<string, unknown>) => request<{ status: string; item: Record<string, unknown> }>(`/api/investment/industry-trends/${id}/decisions`, { method: "POST", body: JSON.stringify(payload) }),
  researchSettings: (id: number) => request<IndustryTrendResearchSetting>(`/api/investment/industry-trends/${id}/research-settings`),
  saveResearchSettings: (id: number, payload: Record<string, unknown>) => request<IndustryTrendResearchSetting>(`/api/investment/industry-trends/${id}/research-settings`, { method: "PUT", body: JSON.stringify(payload) }),
  generate: (payload: Record<string, unknown>) => request<IndustryTrendJob>("/api/investment/industry-trends/generate", { method: "POST", body: JSON.stringify(payload) }),
  jobs: (id: number) => request<{ items: IndustryTrendJob[] }>(`/api/investment/industry-trends/${id}/generation-jobs`),
  job: (jobId: number) => request<IndustryTrendJob>(`/api/investment/industry-trends/generation-jobs/${jobId}`),
  cancelJob: (jobId: number) => request<IndustryTrendJob>(`/api/investment/industry-trends/generation-jobs/${jobId}/cancel`, { method: "POST" }),
  applyDraft: (id: number) => request<IndustryTrendDetail>(`/api/investment/industry-trends/${id}/draft/apply`, { method: "POST" }),
  deleteDraft: (id: number) => request(`/api/investment/industry-trends/${id}/draft`, { method: "DELETE" }),
  versions: (id: number) => request<{ items: Array<{ id: number; revision: number; created_at: string; snapshot: Record<string, unknown> }> }>(`/api/investment/industry-trends/${id}/versions`),
  restore: (id: number, versionId: number) => request<IndustryTrendDetail>(`/api/investment/industry-trends/${id}/versions/${versionId}/restore`, { method: "POST" })
};
