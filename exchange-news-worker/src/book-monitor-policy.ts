export const BOOK_HIGH_FREQUENCY_MS = 5 * 60 * 1000;
export const BOOK_MAX_RECHECK_MS = 2 * 60 * 60 * 1000;

export type BookMonitorPhase = "inactive" | "verify" | "recheck" | "retire";
export type BookMonitorRetireReason = "historical" | "terminal" | "timeout" | "invalid_time" | null;

export interface BookMonitorLike {
  monitorStatus?: string;
  monitorStartedAt?: string | null;
  monitorEndsAt?: string | null;
  notificationPolicy?: string | null;
  lastBookError?: string | null;
}

const TERMINAL_BOOK_ERROR = /invalid symbol|symbol[^\n]{0,40}(?:does not exist|not found|invalid)|instrument[^\n]{0,40}(?:not found|does not exist)|(?:delivering|delivered|settling|settled|closed)\b/i;

export function classifyBookMonitor(event: BookMonitorLike, nowMs: number): {
  phase: BookMonitorPhase;
  reason: BookMonitorRetireReason;
} {
  if (!event || !["verifying", "waiting_book"].includes(event.monitorStatus ?? "")) {
    return { phase: "inactive", reason: null };
  }

  const startedAt = Date.parse(event.monitorStartedAt ?? "");
  const configuredHighFrequencyEnd = Date.parse(event.monitorEndsAt ?? "");
  if (!Number.isFinite(startedAt)) return { phase: "retire", reason: "invalid_time" };
  const highFrequencyEnd = Number.isFinite(configuredHighFrequencyEnd)
    ? configuredHighFrequencyEnd
    : startedAt + BOOK_HIGH_FREQUENCY_MS;

  if (nowMs < highFrequencyEnd) return { phase: "verify", reason: null };
  if (event.notificationPolicy === "historical_only") return { phase: "retire", reason: "historical" };
  if (TERMINAL_BOOK_ERROR.test(event.lastBookError ?? "")) return { phase: "retire", reason: "terminal" };
  if (nowMs >= startedAt + BOOK_MAX_RECHECK_MS) return { phase: "retire", reason: "timeout" };
  return { phase: "recheck", reason: null };
}

