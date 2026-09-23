const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const exportsObject = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/state-storage.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: exportsObject, crypto: require('node:crypto').webcrypto, TextEncoder, Uint8Array });
const { readState, writeState } = exportsObject;

function fakeStorage() {
  let data = new Map(), fail = false;
  const metrics = { writes: 0, batches: 0 };
  const access = map => ({
    get: async key => Array.isArray(key) ? new Map(key.filter(k => map.has(k)).map(k => [k, map.get(k)])) : map.get(key),
    put: async entries => { assert.ok(Object.keys(entries).length <= 100); metrics.batches++; for (const [key, value] of Object.entries(entries)) {
      assert.ok(Buffer.byteLength(JSON.stringify(value)) < 131072); metrics.writes++; map.set(key, value);
      if (fail) { fail = false; throw Error('simulated write failure'); }
    } },
    delete: async keys => { for (const key of Array.isArray(keys) ? keys : [keys]) { metrics.writes++; map.delete(key); } },
  });
  return {
    get: key => access(data).get(key),
    transaction: async fn => { const copy = new Map(data); await fn(access(copy)); data = copy; },
    seed: (key, value) => data.set(key, value), remove: key => data.delete(key), keys: () => [...data.keys()],
    failNext: () => { fail = true; }, metrics,
  };
}

(async () => {
  const storage = fakeStorage(), cache = new Map();
  const state = { version: 1, running: false, inventory: { bn: { BYDUSDT: { symbol: 'BYD', firstSeenAt: 'original' } } },
    announcements: Array.from({ length: 500 }, (_, i) => ({ key: 'article:' + i, detailText: '正文'.repeat(4000), publishedAt: 'original' })),
    events: [{ id: 'original-event', bookChecks: 1, pushedAt: 'original-push' }],
    pushLogs: [{ id: 'sent-once', createdAt: 'original-push' }], runtime: { lastCycleAt: 'old' },
  };
  const legacy = JSON.stringify(state);
  let parts = 0;
  for (let offset = 0; offset < legacy.length; offset += 16000) storage.seed('state:part:' + parts++, legacy.slice(offset, offset + 16000));
  storage.seed('state:parts', parts);
  assert.equal(JSON.stringify(await readState(storage, cache)), legacy);
  assert.equal(storage.metrics.writes, 0, 'legacy reads must be read-only');
  const initialWrites = await writeState(storage, state, cache);
  assert.ok(await storage.get('state:part:0'), 'original checkpoint retained');
  assert.equal(JSON.stringify(await readState(storage)), legacy);
  assert.equal(await writeState(storage, state, cache), 0, 'unchanged state: zero writes');
  state.events[0].bookChecks++;
  const singleRecordWrites = await writeState(storage, state, cache);
  assert.equal(singleRecordWrites, 1, 'book count must not rewrite announcements');
  const reloadedCache = new Map();
  await readState(storage, reloadedCache);
  assert.equal(await writeState(storage, state, reloadedCache), 0, 'cold reload primes diff cache');
  const beforeFailure = JSON.stringify(await readState(storage));
  state.runtime.lastCycleAt = 'new';
  storage.failNext();
  await assert.rejects(writeState(storage, state, cache), /simulated write failure/);
  assert.equal(JSON.stringify(await readState(storage)), beforeFailure, 'failed transaction cannot lose old data');
  assert.equal(await writeState(storage, state, cache), 1, 'failed cache cannot suppress retry');
  state.announcements.unshift({ key: 'new-article', detailText: '新增', publishedAt: 'new' });
  const insertWrites = await writeState(storage, state, cache);
  assert.ok(insertWrites < 15, 'inserting a news row must not shift all body storage');
  state.announcements.reverse();
  const reorderWrites = await writeState(storage, state, cache);
  assert.ok(reorderWrites < 15);
  assert.equal(JSON.stringify(await readState(storage)), JSON.stringify(state));
  const removed = state.announcements.pop();
  await writeState(storage, state, cache);
  assert.equal((await readState(storage)).announcements.length, 500);
  assert.ok(removed);
  const manifestKey = storage.keys().find(key => key.startsWith('state:v3:manifest:'));
  storage.remove(manifestKey);
  await assert.rejects(readState(storage), /禁止重建空基线/);
  console.log(JSON.stringify({ passed: true, payloadBytes: Buffer.byteLength(legacy), initialWrites, unchangedWrites: 0,
    singleRecordWrites, insertWrites, reorderWrites, transactionalRollback: true, originalStatePreserved: true }));
})().catch(error => { console.error(error); process.exitCode = 1; });
