const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

const exportsObject = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/book-monitor-policy.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: exportsObject });

const { classifyBookMonitor } = exportsObject;
const startedAt = '2026-09-18T00:00:00.000Z';
const base = {
  monitorStatus: 'verifying',
  monitorStartedAt: startedAt,
  monitorEndsAt: '2026-09-18T00:05:00.000Z',
};

assert.equal(classifyBookMonitor(base, Date.parse('2026-09-18T00:04:59.000Z')).phase, 'verify');
assert.equal(classifyBookMonitor(base, Date.parse('2026-09-18T00:05:00.000Z')).phase, 'recheck');
assert.equal(JSON.stringify(classifyBookMonitor({ ...base, notificationPolicy: 'historical_only' }, Date.parse('2026-09-18T00:05:00.000Z'))), JSON.stringify({ phase: 'retire', reason: 'historical' }));
assert.equal(JSON.stringify(classifyBookMonitor({ ...base, lastBookError: 'Error: Invalid symbol.' }, Date.parse('2026-09-18T00:05:01.000Z'))), JSON.stringify({ phase: 'retire', reason: 'terminal' }));
assert.equal(JSON.stringify(classifyBookMonitor(base, Date.parse('2026-09-18T02:00:00.000Z'))), JSON.stringify({ phase: 'retire', reason: 'timeout' }));
assert.equal(JSON.stringify(classifyBookMonitor({ ...base, monitorStartedAt: null }, Date.parse('2026-09-18T00:00:00.000Z'))), JSON.stringify({ phase: 'retire', reason: 'invalid_time' }));
assert.equal(classifyBookMonitor({ ...base, monitorStatus: 'tradable' }, Date.now()).phase, 'inactive');

console.log(JSON.stringify({ passed: true, policy: '5m-high-frequency/2h-hard-cap', terminalErrorsRetired: true, historicalTasksRetired: true }));
