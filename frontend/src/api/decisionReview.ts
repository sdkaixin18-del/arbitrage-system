import { request } from "../apiClient";

export type DecisionCode = "A" | "B" | "C" | "D";
export type PolicyGateState = "pass" | "wait" | "fail" | "missing";

export interface DecisionPolicyGate {
  key: string;
  label: string;
  state: PolicyGateState;
  summary: string;
  facts: string[];
}

export interface DecisionPolicyRecommendation {
  policy_id: string;
  version: string;
  label: string;
  recommendation: DecisionCode;
  reason: string;
  scope: string;
  gates: DecisionPolicyGate[];
}

export interface DecisionPolicyQualityCheck {
  key: string;
  label: string;
  passed: boolean;
  message: string;
}

export interface DecisionPolicyEvaluation {
  policies: DecisionPolicyRecommendation[];
  disagreement: boolean;
  quality_checks: DecisionPolicyQualityCheck[];
  quality_complete: boolean;
  principle: string;
  manual_code?: DecisionCode;
  manual_disagreements?: Array<{
    policy_id: string;
    policy_label: string;
    policy_code: DecisionCode;
    manual_code: DecisionCode;
  }>;
  manual_aligned_with_all?: boolean;
}

export interface HorizonPerformance {
  available: boolean;
  bars_available?: number;
  exit_date?: string;
  exit_price?: number;
  gross_return_pct?: number;
  return_pct?: number;
  benchmark_return_pct?: number | null;
  excess_return_pct?: number | null;
  mfe_pct?: number;
  mae_pct?: number;
}

export interface DecisionPerformance {
  entry_date?: string;
  entry_price?: number;
  bars_available?: number;
  cost_bps?: number;
  horizons?: Record<string, HorizonPerformance>;
  benchmark_status?: string;
}

export interface DecisionEvent {
  id: number;
  decision_key: string;
  source_kind: "information" | "industry_trend";
  industry_chain_id: number | null;
  information_item_id: number | null;
  information_item_key: string | null;
  information_title: string;
  source_type: string;
  source_name: string;
  snapshot: Record<string, unknown>;
  policy_evaluation: DecisionPolicyEvaluation;
  signal_at: string;
  decision_code: DecisionCode;
  primary_stock_code: string | null;
  primary_stock_name: string | null;
  primary_full_code: string | null;
  alternatives: string[];
  direction_verdict: string;
  stock_verdict: string;
  timing_verdict: string;
  pricing_verdict: string;
  thesis: string;
  why_best: string;
  trigger_conditions: string[];
  invalidation_conditions: string[];
  planned_horizon: 5 | 20 | 60;
  benchmark_code: string;
  cost_bps: number;
  execution_status: string;
  entry_date: string | null;
  entry_price: number | null;
  performance: DecisionPerformance;
  created_at: string;
  updated_at: string;
}

export interface BacktestMetric {
  sample_count: number;
  win_rate: number | null;
  avg_return_pct: number | null;
  expected_value_pct: number | null;
  payoff_ratio: number | null;
  avg_excess_return_pct: number | null;
  avg_mfe_pct: number | null;
  avg_mae_pct: number | null;
}

export interface DecisionOverview {
  status: string;
  items: DecisionEvent[];
  summary: {
    total: number;
    decision_counts: Record<DecisionCode, number>;
    policy_counts: Record<string, Record<DecisionCode, number>>;
    policy_disagreement_total: number;
    manual_disagreement_total: number;
    quality_incomplete_total: number;
    waiting: number;
    tracking: number;
    matured: number;
    pipeline: {
      information_total: number;
      verified_total: number;
      decision_total: number;
      industry_decision_total: number;
      simulated_total: number;
    };
    horizons: Record<string, BacktestMetric>;
    calibration_20d: Record<DecisionCode, BacktestMetric>;
    method: Record<string, string>;
  };
}

export interface DecisionCreateInput {
  information_item_id: number;
  decision_code: DecisionCode;
  primary_stock_code?: string;
  primary_stock_name?: string;
  alternatives?: string[];
  direction_verdict?: string;
  stock_verdict?: string;
  pricing_verdict: string;
  thesis: string;
  why_best?: string;
  trigger_conditions?: string[];
  invalidation_conditions?: string[];
  planned_horizon: 5 | 20 | 60;
  cost_bps?: number;
}

export interface FiveYearMetric {
  sample_count: number;
  unique_symbols: number;
  unique_signal_dates: number;
  win_rate_pct: number | null;
  avg_return_pct: number | null;
  avg_excess_return_pct: number | null;
  median_excess_return_pct: number | null;
  payoff_ratio: number | null;
  bootstrap_95ci_low_pct?: number | null;
  bootstrap_95ci_high_pct?: number | null;
}

export interface FiveYearMarketResult {
  thresholds: Record<string, number>;
  threshold_selection: Record<string, unknown>;
  development: {
    by_decision: Record<DecisionCode, FiveYearMetric>;
    pricing_gain_a_vs_b_median_pct: number | null;
    filter_gain_a_vs_bc_median_pct: number | null;
    by_year_A: Record<string, FiveYearMetric>;
  };
  validation: {
    by_decision: Record<DecisionCode, FiveYearMetric>;
    pricing_gain_a_vs_b_median_pct: number | null;
    filter_gain_a_vs_bc_median_pct: number | null;
    by_year_A: Record<string, FiveYearMetric>;
  };
  verdict: {
    state: "validated" | "useful_with_limits" | "not_validated";
    checks: Record<string, boolean>;
    interpretation: string;
  };
  event_counts: Record<string, number>;
}

export interface FiveYearValidationSummary {
  status: string;
  method_version: string;
  generated_at: string;
  period: { start: string; end: string; years: number };
  validation_start: string;
  overall_verdict: {
    state: "validated" | "useful_with_limits" | "not_validated";
    interpretation: string;
  };
  markets: Record<"A股" | "美股", FiveYearMarketResult>;
  coverage: Record<string, Record<string, number>>;
  errors: Record<string, string[]>;
  artifacts: Record<string, string>;
}

export interface FiveYearValidationOverview {
  status: {
    status: string;
    stage?: string;
    progress_pct?: number;
    message?: string;
    updated_at?: string;
  };
  summary: FiveYearValidationSummary | null;
  source_manifest: {
    sources?: Array<{ name: string; role: string; url: string; tier: string }>;
    rules?: Record<string, string>;
    limitations?: string[];
  } | null;
  artifacts: Record<string, string | null>;
}

export interface FiveYearEventRow {
  event_key: string;
  market: "A股" | "美股";
  symbol: string;
  name: string;
  signal_date: string;
  report_period: string;
  evidence_grade: string;
  profit_growth_pct: string;
  growth_acceleration_pct: string;
  pre_20d_excess_pct: string;
  market_confirmation_excess_pct: string;
  decision_code: DecisionCode;
  evidence_state: string;
  pricing_state: string;
  observation_date: string;
  observation_deferred_sessions: string;
  entry_date: string;
  deferred_sessions: string;
  market_excess_return_pct_20d: string;
  peer_excess_return_pct_20d: string;
  source_url: string;
}

export const decisionReviewApi = {
  overview: () => request<DecisionOverview>("/api/investment/decision-review"),
  create: (payload: DecisionCreateInput) => request<{ status: string; item: DecisionEvent }>(
    "/api/investment/decision-review",
    { method: "POST", body: JSON.stringify(payload) }
  ),
  refresh: () => request<DecisionOverview>(
    "/api/investment/decision-review/refresh",
    { method: "POST" }
  ),
  fiveYearValidation: () => request<FiveYearValidationOverview>(
    "/api/investment/decision-review/five-year-validation"
  ),
  refreshFiveYearValidation: () => request<{ status: string; run: Record<string, unknown> }>(
    "/api/investment/decision-review/five-year-validation/refresh",
    { method: "POST" }
  ),
  fiveYearEvents: () => request<{ status: string; items: FiveYearEventRow[] }>(
    "/api/investment/decision-review/five-year-validation/events?partition=validation&limit=120"
  )
};
