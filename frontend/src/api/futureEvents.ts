import { request } from "../apiClient";

export type FutureEventPriority = "S" | "A" | "B";
export type FutureEventStage = "clue" | "window" | "date_locked" | "time_locked" | "completed";
export type FutureEventSourceStatus = "official" | "media" | "zsxq" | "xueqiu" | "mixed" | "unverified";
export type FutureEventPricedInStatus = "no" | "partial" | "yes" | "unknown";

export interface FutureEventStockMapping {
  market: string;
  code: string;
  name: string;
  role: string;
}

export interface FutureEventStageHistoryItem {
  stage: FutureEventStage;
  at: string;
  note: string;
}

export interface FutureEventItem {
  id: number;
  event_key: string;
  title: string;
  category: string;
  priority: FutureEventPriority;
  stage: FutureEventStage;
  start_date: string;
  end_date: string | null;
  exact_time: string | null;
  timezone: string;
  source_status: FutureEventSourceStatus;
  source_summary: string;
  source_urls: string[];
  market_scope: string;
  stock_mappings: FutureEventStockMapping[];
  impact_chain: string;
  priced_in_status: FutureEventPricedInStatus;
  price_expression: string;
  validation_points: string[];
  invalidation_conditions: string[];
  stage_history: FutureEventStageHistoryItem[];
  conversation_thread_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface FutureEventListResponse {
  status: string;
  today: string;
  days: number | null;
  items: FutureEventItem[];
}

export interface FutureEventFeedbackItem {
  id: number;
  event_key: string;
  title: string;
  category: string;
  priority: string;
  stage: string;
  action: string;
  reason_category: string | null;
  note: string | null;
  snapshot: Record<string, unknown>;
  conversation_thread_id: string | null;
  created_at: string;
}

export interface FutureEventDeletePayload {
  reason_category?: string;
  reason?: string;
}

const deleteRetryDelays = [0, 500, 1_000, 1_500, 2_000];

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function isTemporaryConnectionError(error: unknown): boolean {
  const message = String(error);
  return error instanceof TypeError
    || message.includes("Failed to fetch")
    || message.includes("Backend unavailable")
    || message.includes("Remote end closed connection");
}

export function listFutureEvents(range: "7" | "14" | "30" | "all"): Promise<FutureEventListResponse> {
  const query = range === "all" ? "days=3650&include_past=true" : `days=${range}`;
  return request<FutureEventListResponse>(`/api/future-events?${query}`);
}

export function listFutureEventFeedback(): Promise<FutureEventFeedbackItem[]> {
  return request<FutureEventFeedbackItem[]>("/api/future-events/feedback?limit=500");
}

export async function deleteFutureEvent(eventId: number, payload?: FutureEventDeletePayload): Promise<{ status: string; message: string }> {
  let latestError: unknown;
  for (const delay of deleteRetryDelays) {
    if (delay) await wait(delay);
    try {
      return await request(`/api/future-events/${eventId}`, {
        method: "DELETE",
        body: JSON.stringify(payload ?? {})
      });
    } catch (error) {
      latestError = error;
      if (!isTemporaryConnectionError(error)) throw error;
    }
  }
  throw latestError;
}
