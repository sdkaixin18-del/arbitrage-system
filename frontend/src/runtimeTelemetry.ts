export interface RuntimeErrorReport {
  event: string;
  level?: "warning" | "error";
  message: string;
  module?: string;
  errorType?: string;
  stack?: string;
  details?: Record<string, unknown>;
}

const REPORT_ENDPOINT = "/api/system/runtime-logs/frontend";
const recentReports = new Map<string, number>();

function currentModule() {
  const path = window.location.pathname.replace(/^\/+/, "");
  return path || "home";
}

function shouldReport(signature: string) {
  const now = Date.now();
  const lastAt = recentReports.get(signature) ?? 0;
  if (now - lastAt < 30_000) return false;
  recentReports.set(signature, now);
  if (recentReports.size > 100) {
    for (const [key, at] of recentReports) {
      if (now - at > 60_000) recentReports.delete(key);
    }
  }
  return true;
}

export async function reportRuntimeError(report: RuntimeErrorReport) {
  const signature = `${report.event}|${report.module ?? currentModule()}|${report.message}`;
  if (!shouldReport(signature)) return;
  try {
    await fetch(REPORT_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...report,
        level: report.level ?? "error",
        module: report.module ?? currentModule(),
        page: `${window.location.pathname}${window.location.search}`,
        details: {
          ...report.details,
          userAgent: navigator.userAgent
        }
      }),
      keepalive: true
    });
  } catch {
    // Error reporting must never cause another user-facing error.
  }
}

export function installRuntimeTelemetry() {
  window.addEventListener("error", (event) => {
    void reportRuntimeError({
      event: "browser_uncaught_error",
      message: event.message || "浏览器发生未捕获错误",
      errorType: event.error?.name,
      stack: event.error?.stack,
      details: {
        filename: event.filename,
        line: event.lineno,
        column: event.colno
      }
    });
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    void reportRuntimeError({
      event: "browser_unhandled_rejection",
      message: reason instanceof Error ? reason.message : String(reason ?? "未处理的异步错误"),
      errorType: reason instanceof Error ? reason.name : "UnhandledPromiseRejection",
      stack: reason instanceof Error ? reason.stack : undefined
    });
  });
}
