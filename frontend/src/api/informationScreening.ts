import { request } from "../apiClient";
import type { DecisionPolicyEvaluation } from "./decisionReview";

export type ScreeningBucket = "verified" | "eye_catching" | "filtered";
export type ScreeningVerificationStatus = "verified" | "cross_verified" | "partial" | "unverified" | "disproved";
export type ScreeningPriceStatus = "untraded" | "first_expression" | "multi_rounds" | "negative" | "no_confirmation" | "unknown";

export interface InformationScreeningStats {
  read: number;
  deduplicated: number;
  top: number;
  verified: number;
  eye_catching: number;
  filtered: number;
  official_required: number;
  official_matched: number;
  official_unresolved: number;
}

export interface InformationScreeningBatch {
  id: number;
  batch_key: string;
  title: string;
  window_start: string | null;
  window_end: string | null;
  source_scope: string[];
  read_count: number;
  deduplicated_count: number;
  conversation_thread_id: string | null;
  stats: InformationScreeningStats;
  created_at: string;
  updated_at: string;
}

export interface InformationScreeningItem {
  id: number;
  batch_id: number;
  item_key: string;
  title: string;
  summary: string;
  source_type: string;
  source_name: string;
  source_url: string | null;
  source_urls: string[];
  published_at: string | null;
  bucket: ScreeningBucket;
  is_top: boolean;
  importance: number;
  marginal_change: string;
  verification_status: ScreeningVerificationStatus;
  evidence_summary: string;
  related_sectors: string[];
  related_stocks: string[];
  price_status: ScreeningPriceStatus;
  price_summary: string;
  validation_points: string[];
  invalidation_conditions: string[];
  filter_reason: string;
  recovery_condition: string;
  official_check_status: "not_required" | "matched" | "not_found" | "error" | "missing_stock";
  official_check_message: string;
  official_source_url: string | null;
  official_stock_codes: string[];
  official_checked_at: string | null;
  promoted_chain_id: number | null;
  conversation_thread_id: string | null;
  decision_policy: DecisionPolicyEvaluation;
  created_at: string;
  updated_at: string;
}

export interface InformationScreeningOverview {
  status: string;
  batch: InformationScreeningBatch | null;
  batches: InformationScreeningBatch[];
  items: InformationScreeningItem[];
}

export interface InformationScreeningFeedbackItem {
  id: number;
  item_key: string;
  batch_key: string;
  title: string;
  source_type: string;
  source_name: string;
  bucket: ScreeningBucket;
  verification_status: ScreeningVerificationStatus;
  action: string;
  reason_category: string | null;
  note: string | null;
  snapshot: InformationScreeningItem;
  conversation_thread_id: string | null;
  created_at: string;
}

export interface InformationScreeningDeletePayload {
  reason_category?: string;
  note?: string;
}

export const informationScreeningApi = {
  overview: (batchId?: number) => {
    const suffix = batchId ? `?batch_id=${batchId}` : "";
    return request<InformationScreeningOverview>(`/api/investment/information-screening${suffix}`);
  },
  upsertBatch: (payload: Record<string, unknown>) => request<InformationScreeningOverview>(
    "/api/investment/information-screening/batches",
    { method: "POST", body: JSON.stringify(payload) }
  ),
  patchItem: (itemId: number, payload: Record<string, unknown>) => request<{ status: string; item: InformationScreeningItem }>(
    `/api/investment/information-screening/items/${itemId}`,
    { method: "PATCH", body: JSON.stringify(payload) }
  ),
  feedback: () => request<{ status: string; items: InformationScreeningFeedbackItem[] }>(
    "/api/investment/information-screening/feedback?limit=500"
  ),
  deleteItem: (itemId: number, payload: InformationScreeningDeletePayload) => request<{ status: string; message: string; feedback_rule: string }>(
    `/api/investment/information-screening/items/${itemId}`,
    { method: "DELETE", body: JSON.stringify(payload) }
  ),
  promoteItem: (itemId: number, chainId: number) => request<{ status: string; message: string; item: InformationScreeningItem }>(
    `/api/investment/information-screening/items/${itemId}/promote`,
    { method: "POST", body: JSON.stringify({ chain_id: chainId }) }
  )
};
