import { request } from "../apiClient";

export type DecisionCode = "A" | "B" | "C" | "D";
export type DecisionGateStatus = "pass" | "wait" | "fail";

export interface DecisionFlowStep {
  key: string;
  number: string;
  label: string;
  status: string;
  detail: string;
}

export interface DecisionFlowGate {
  key: string;
  label: string;
  layer: "discover" | "confirm";
  status: DecisionGateStatus;
}

export interface DecisionFlowItem {
  key: string;
  decision_code: DecisionCode;
  decision_label: string;
  name: string;
  source: "产业研究" | "市场候选";
  primary_company: string | null;
  cycle: string;
  pricing_status: string;
  current_action: string;
  trigger: string;
  invalidation: string;
  link: string;
  link_label: string;
  gates: DecisionFlowGate[];
  facts: string[];
}

export interface DecisionFlowResponse {
  status: "ok" | "partial";
  message?: string;
  as_of: string;
  data_status: "current" | "stale";
  lag_days: number;
  headline: {
    market_style: string;
    risk_budget: string;
    action: string;
    style_action: string;
  };
  market_metrics: {
    market_member_breadth?: number;
    industry_index_breadth?: number;
    high_low_beta_spread_20d?: number;
  };
  steps: DecisionFlowStep[];
  counts: Record<DecisionCode, number>;
  queues: Record<DecisionCode, DecisionFlowItem[]>;
  rule: string;
  decision_definitions: Record<DecisionCode, string>;
}

export const decisionFlowApi = {
  overview: (refresh = false) =>
    request<DecisionFlowResponse>(
      `/api/investment/decision-flow${refresh ? "?refresh=true" : ""}`
    )
};
