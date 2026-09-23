const assert = require('node:assert/strict');
const { mkdtemp, rm } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { Miniflare, convertV4MiniflareOptions } = require('miniflare');

(async () => {
  const path = await mkdtemp(join(tmpdir(), 'news-migration-test-'));
  let outbound = 0;
  const make = () => {
    const options = convertV4MiniflareOptions({ name: 'migration-test', modules: true, scriptPath: 'dist/index.js', compatibilityDate: '2026-09-02', compatibilityFlags: ['nodejs_compat'], kvNamespaces: ['STATE_KV'], durableObjects: { MONITOR: { className: 'ExchangeMonitor', useSQLite: true } }, bindings: { ADMIN_TOKEN: 'test', CONTROL_TOKEN: 'control-test' }, outboundService: () => { outbound++; return new Response('test blocked', { status: 503 }); } });
    options.resourcePersistencePath = path;
    return new Miniflare(options);
  };
  let mf = make();
  const call = (name, method = 'POST', body) => mf.dispatchFetch('http://local/control/' + name, { method, headers: { authorization: 'Bearer control-test', 'content-type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
  try {
    assert.equal((await mf.dispatchFetch('http://local/control/backup')).status, 401);
    assert.equal((await call('start')).status, 409);
    assert.equal((await mf.dispatchFetch('http://local/health')).status, 503);
    const seed = { version: 1, state: { version: 1, running: false, repairVersion: 999, migrationCutoffAt: '2026-09-08T00:00:00Z', migrationPendingSources: ['bn'], inventory: { bn: {}, bg: {}, by: {}, gate: {}, okx: {}, aster: {}, hl: {} }, announcements: [], events: [], pushLogs: [], activityLogs: [], newsReadLogs: [], newsHourlyStats: [] }, locks: { 'push-lock:fixture': { status: 'sent', token: 'fixture', eventId: 'fixture', updatedAt: '2026-09-08T00:00:00Z' } } };
    assert.equal((await call('import', 'POST', seed)).status, 200);
    let exported = await (await call('export', 'GET')).json();
    assert.equal(exported.locks['push-lock:fixture'].status, 'sent');
    await mf.dispose(); mf = make();
    exported = await (await call('export', 'GET')).json();
    assert.equal(exported.state.running, false);
    assert.equal(exported.state.migrationCutoffAt, seed.state.migrationCutoffAt);
    assert.equal(exported.locks['push-lock:fixture'].status, 'sent');
    const kv = await mf.getKVNamespace('STATE_KV');
    await kv.put('execution-lease:v1', JSON.stringify({ until: Date.now() - 1 }));
    assert.equal((await call('start')).status, 409);
    assert.equal(outbound, 0);
    console.log(JSON.stringify({ passed: true, restartedStoragePreserved: true, pushLocksPreserved: true, cloudWithoutLeaseBlocked: true, outbound }));
  } finally { await mf.dispose(); await rm(path, { recursive: true, force: true }); }
})().catch(error => { console.error(error); process.exitCode = 1; });
