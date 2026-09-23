const assert = require('node:assert/strict');
const fs = require('node:fs');
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');

(async () => {
  let outbound = 0;
  const mf = new Miniflare(convertV4MiniflareOptions({
    name: 'runtime-audit', modules: true, scriptPath: 'dist/index.js',
    compatibilityDate: '2026-09-02', compatibilityFlags: ['nodejs_compat'],
    kvNamespaces: ['STATE_KV'],
    durableObjects: { MONITOR: { className: 'ExchangeMonitor', useSQLite: true } },
    bindings: { ADMIN_TOKEN: 'isolated-test-only' },
    outboundService: () => { outbound++; return new Response('blocked in test', { status: 503 }); },
  }));
  try {
    const kv = await mf.getKVNamespace('STATE_KV');
    const fixture = {
      version: 1, running: false,
      inventory: { bn: {}, bg: {}, by: {}, gate: {}, okx: {}, aster: {}, hl: {} },
      announcements: [], events: [], pushLogs: [], activityLogs: [], newsReadLogs: [], newsHourlyStats: [],
      testLarge: '中文'.repeat(1200000),
    };
    await kv.put('monitor-state-v2', JSON.stringify(fixture));
    const snapshot = await (await mf.dispatchFetch('http://local/snapshot')).json();
    assert.equal(snapshot.status, 'stopped');
    assert.equal(snapshot.revision, 'delisting-reminders-2026-09-06-r2');
    assert.equal(snapshot.cardIntegration, undefined, 'retired auto-card integration must stay removed');
    const denied = await mf.dispatchFetch('http://local/proxy/contracts?exchange=bn');
    assert.equal(denied.status, 401);
    const stopped = await mf.dispatchFetch('http://local/admin/scan', {
      method: 'POST', headers: { Authorization: 'Bearer isolated-test-only' },
    });
    assert.equal(stopped.status, 409);
    assert.equal(outbound, 0);
    for (const path of ['app/layout.tsx', 'app/api/monitor/route.ts']) {
      assert.equal(fs.readFileSync('../exchange-news-site/' + path, 'utf8').includes('/admin/start'), false);
    }
    console.log(JSON.stringify({
      passed: true, migrationBytes: Buffer.byteLength(JSON.stringify(fixture)),
      stoppedPreserved: true, proxyAuthenticated: true, pageReadOnly: true, outboundRequests: outbound,
    }));
  } finally { await mf.dispose(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
