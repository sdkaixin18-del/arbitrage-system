import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

type MonitorHealth = {
  status?: string;
  runtime?: { nextAlarmAt?: string | null };
  [key: string]: unknown;
};

function configuration() {
  const baseUrl = process.env.MONITOR_API_URL?.replace(/\/$/, '');
  if (!baseUrl) throw new Error('云端监控服务环境变量未配置。');
  return { baseUrl };
}

async function readSnapshot(baseUrl: string): Promise<MonitorHealth> {
  const response = await fetch(`${baseUrl}/health`, { cache: 'no-store', signal: AbortSignal.timeout(15_000) });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { error?: string; code?: string; retryAt?: string | null } | null;
    const failure = new Error(payload?.error ?? `监控服务返回 HTTP ${response.status}`);
    Object.assign(failure, { code: payload?.code, retryAt: payload?.retryAt });
    throw failure;
  }
  const payload = await response.json() as MonitorHealth;
  if (!Array.isArray(payload.listingReminders) || !Array.isArray(payload.activityLogs)) throw new Error('监控服务未返回有效数据，请稍后重试。');
  return payload;
}

export async function GET() {
  try {
    const { baseUrl } = configuration();
    // Viewing the page must never override an explicit operator stop.
    const snapshot = await readSnapshot(baseUrl);
    return NextResponse.json(snapshot, { headers: { 'cache-control': 'no-store' } });
  } catch (error) {
    const detail = error as Error & { code?: string; retryAt?: string | null };
    return NextResponse.json(
      { error: error instanceof Error ? error.message : String(error), code: detail?.code ?? 'MONITOR_UNAVAILABLE', retryAt: detail?.retryAt ?? null },
      { status: 503, headers: { 'cache-control': 'no-store' } },
    );
  }
}
