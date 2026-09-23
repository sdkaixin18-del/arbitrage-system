// Kept independent of network/process state for deterministic failover tests.
export function healthySnapshot(snapshot, now = Date.now()) {
  if (!['running', 'degraded'].includes(snapshot?.status) || snapshot.runtime?.lastError) return false;
  const cycle = Date.parse(snapshot.runtime?.lastCycleAt ?? '');
  if (!Number.isFinite(cycle) || now - cycle > 90_000 || cycle > now + 10_000) return false;
  const sources = snapshot.sources ?? [];
  return sources.some(s => s.kind === 'contracts' && s.status === 'ok' && now - Date.parse(s.lastCheckedAt) < 90_000) &&
    sources.some(s => s.kind === 'news' && ['ok', 'degraded'].includes(s.status) && now - Date.parse(s.lastCheckedAt) < 180_000);
}

function mergeRows(previous = [], incoming = [], key, limit) {
  const merged = new Map(previous.map(row => [key(row), row]));
  for (const row of incoming) merged.set(key(row), { ...merged.get(key(row)), ...row });
  const at = row => Date.parse(row.updatedAt ?? row.createdAt ?? row.at ?? row.responseCompletedAt ?? row.publishedAt ?? row.fetchedAt ?? '') || 0;
  return [...merged.values()].sort((a, b) => at(b) - at(a)).slice(0, limit);
}

export function mergeReplica(payload, snapshot) {
  if (!snapshot || !Array.isArray(snapshot.announcements) || !Array.isArray(snapshot.events)) return structuredClone(payload);
  const next = structuredClone(payload);
  const state = next.state;
  const announcements = snapshot.announcements.map(row => ({ ...row, key: row.key ?? row.id }));
  state.announcements = mergeRows(state.announcements, announcements, row => row.key, 500);
  state.events = mergeRows(state.events, snapshot.events, row => row.id, 240);
  for (const [field, limit] of [['pushLogs', 120], ['activityLogs', 120], ['newsReadLogs', 240]]) {
    state[field] = mergeRows(state[field], snapshot[field], row => row.id, limit);
  }
  return next;
}

export function resumeBaseline(payload, now = Date.now()) {
  const next = structuredClone(payload);
  next.state.migrationCutoffAt = new Date(now).toISOString();
  next.state.migrationPendingSources = ['bn', 'bg', 'by', 'gate', 'okx', 'aster', 'hl'];
  // Rate limits belong to the old network egress. Probe once on the new owner,
  // then let that owner's own source backoff take over if it is also blocked.
  next.state.newsRetryState = {};
  for (const event of next.state.events) {
    if (!event.pushedAt) { event.notificationPolicy = 'historical_only'; event.recoveredAfterGap = true; event.pushStatus = 'suppressed'; }
  }
  return next;
}
