// Transport success is not enough: failed scans still advance the scheduler.
export function localUnavailable(snapshot, now = Date.now()) {
  const cycle = Date.parse(snapshot?.runtime?.lastCycleAt ?? '');
  if (snapshot?.runtime?.lastError || !Number.isFinite(cycle) || now - cycle > 90_000 || cycle > now + 10_000) return true;
  return ['contracts', 'news'].some(kind => !(snapshot.sources ?? []).some(source =>
    source.kind === kind && ['ok', 'degraded'].includes(source.status) &&
    (source.consecutiveFailures ?? 0) === 0 &&
    now - Date.parse(source.lastCheckedAt) < (kind === 'news' ? 180_000 : 90_000)));
}

export function shouldRestartLocal({ failures, startedAt, lastRestartAt, now = Date.now() }) {
  return failures >= 3 && now - startedAt >= 120_000 && now - lastRestartAt >= 600_000;
}
