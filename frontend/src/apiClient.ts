import { reportRuntimeError } from "./runtimeTelemetry";

export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const isFormData = typeof FormData !== "undefined" && init?.body instanceof FormData;
  const startedAt = performance.now();
  let response: Response;
  try {
    response = await fetch(url, {
      headers: isFormData
        ? { ...(init?.headers ?? {}) }
        : {
            "Content-Type": "application/json",
            ...(init?.headers ?? {})
          },
      ...init
    });
  } catch (error) {
    if (!url.startsWith("/api/system/runtime-logs")) {
      void reportRuntimeError({
        event: "api_network_error",
        message: error instanceof Error ? error.message : String(error),
        errorType: error instanceof Error ? error.name : "NetworkError",
        stack: error instanceof Error ? error.stack : undefined,
        details: { url, method: init?.method ?? "GET", durationMs: performance.now() - startedAt }
      });
    }
    throw error;
  }
  if (!response.ok) {
    const text = await response.text();
    const error = new Error(text || response.statusText);
    if (response.status >= 500 && !url.startsWith("/api/system/runtime-logs")) {
      void reportRuntimeError({
        event: "api_response_error",
        message: error.message,
        errorType: "HttpError",
        stack: error.stack,
        details: {
          url,
          method: init?.method ?? "GET",
          status: response.status,
          durationMs: performance.now() - startedAt
        }
      });
    }
    throw error;
  }
  return response.json() as Promise<T>;
}
