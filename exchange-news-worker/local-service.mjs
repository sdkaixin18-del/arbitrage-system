import { createServer } from 'node:http';
import { readFile, mkdir, writeFile, stat } from 'node:fs/promises';
import { appendFileSync, existsSync, renameSync, statSync } from 'node:fs';
import { resolve, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { parseEnv } from 'node:util';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { fetch as externalFetch, ProxyAgent } from 'undici';
import { healthySnapshot, mergeReplica, resumeBaseline } from './failover.mjs';
import { localUnavailable, shouldRestartLocal } from './local-health.mjs';
import { forwardOutbound, singleFlightReader } from './local-transport.mjs';

const root = fileURLToPath(new URL('.', import.meta.url));
const data = process.env.NEWS_DATA_DIR || '/home/example/Documents/套利系统/runtime/exchange-news';
const cloud = 'https://news.example.invalid';
const host = '127.0.0.1';
const port = Number(process.env.NEWS_PORT || 8790);
const secretsPath = '/home/example/Documents/套利系统/runtime/secrets/exchange-news-control.json';
await mkdir(data, { recursive: true, mode: 0o700 });
const logPath = resolve(data, 'service.log');
const recordLog = (...args) => {
  try {
    if (existsSync(logPath) && statSync(logPath).size > 5 * 1024 * 1024) {
      for (let index = 2; index >= 1; index--) if (existsSync(logPath + '.' + index)) renameSync(logPath + '.' + index, logPath + '.' + (index + 1));
      renameSync(logPath, logPath + '.1');
    }
    appendFileSync(logPath, args.map(value => typeof value === 'string' ? value : JSON.stringify(value)).join(' ') + '\n', { mode: 0o600 });
  } catch { /* Logging must not interrupt persistence or monitoring. */ }
};
console.log = recordLog; console.warn = recordLog; console.error = recordLog;
const secretStat = await stat(secretsPath);
if (secretStat.mode & 0o077) throw new Error('新闻控制配置权限必须为 600');
const { controlToken } = JSON.parse(await readFile(secretsPath, 'utf8'));
const legacyEnv = parseEnv(await readFile('/home/example/Library/Application Support/stock-review-mac-launcher/runtime.env', 'utf8'));
const bark = legacyEnv.BARK_WEBHOOK_URL || legacyEnv.BARK_URL;
if (!bark) throw new Error('未找到已有推送通道，未启用监控');
const proxy = process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
const dispatcher = proxy ? new ProxyAgent(proxy) : undefined;
const network = (url, init = {}) => externalFetch(url, { ...init, ...(dispatcher ? { dispatcher } : {}), signal: init.signal || AbortSignal.timeout(30_000) });
const db = new DatabaseSync(resolve(data, 'control.sqlite'));
db.exec('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS read_marks(url TEXT PRIMARY KEY,read INTEGER NOT NULL,updated_at TEXT NOT NULL);');
const get = (key, fallback) => { const row = db.prepare('SELECT value FROM settings WHERE key=?').get(key); return row ? JSON.parse(row.value) : fallback; };
const save = (key, value) => db.prepare('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE value<>excluded.value').run(key, JSON.stringify(value));
let mode = get('mode', 'paused');
let policy = get('policy', 'manual');
let failures = 0;
let recoveryChecks = 0;
let nextRecoveryAt = get('nextRecoveryAt', 0);
let watchdogBusy = false;
let busy = true;
let message = '本地服务正在恢复';
let mf;
let switchTask;
const status = () => ({ mode, policy, busy, message: quotaStatusMessage() || message, cloudQuota: get('cloudQuotaHold', null), cloudRequiresLocalController: true, replicaAt: get('replicaAt', null), nextRecoveryAt: policy === 'auto' && mode !== 'cloud' ? nextRecoveryAt : null });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const responseJson = async response => {
  const result = await response.json().catch(() => { throw new Error(`服务返回 HTTP ${response.status}，未提供有效数据`); });
  if (!response.ok) {
    const error = new Error(result.error || `服务返回 HTTP ${response.status}`);
    const retryAfter = response.headers.get('retry-after');
    const headerAt = retryAfter && (/^\d+(?:\.\d+)?$/.test(retryAfter) ? Date.now() + Number(retryAfter) * 1000 : Date.parse(retryAfter));
    const bodyAt = Date.parse(result.retryAt);
    Object.assign(error, { code: result.code, status: response.status,
      retryAt: Number.isFinite(bodyAt) ? bodyAt : Number.isFinite(headerAt) ? headerAt : null,
      retrySource: Number.isFinite(bodyAt) ? result.retrySource || 'server' : Number.isFinite(headerAt) ? 'retry-after' : null });
    throw error;
  }
  return result;
};
const isCloudQuota = error => ['STORAGE_DAILY_QUOTA_EXCEEDED', 'CLOUD_QUOTA_EXCEEDED'].includes(error?.code);
const beijingTime = time => new Date(time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
function quotaStatusMessage() {
  const hold = get('cloudQuotaHold', null);
  if (!hold || mode === 'cloud' || busy) return null;
  const prefix = mode === 'local' ? '本地持续运行' : '监控已暂停';
  const timing = hold.retryAt ? `${hold.retrySource === 'daily-reset-estimate' ? '预计额度重置' : '服务返回重试时间'}：北京时间 ${beijingTime(hold.retryAt)}` : '来源未提供额度恢复时间';
  const action = policy !== 'auto' ? '当前为手动模式，不自动切回' : Date.now() < nextRecoveryAt ? `下次验证：北京时间 ${beijingTime(nextRecoveryAt)}` : '等待验证，尚未确认恢复';
  return `${prefix}；云端额度不足；${timing}；${action}。到点不代表已恢复。`;
}
function quotaGate() {
  const hold = get('cloudQuotaHold', null);
  if (hold && Date.now() < nextRecoveryAt) throw Object.assign(new Error(hold.reason), hold, { quotaHeld: true });
}
const modeName = value => ({ local: '本地', cloud: '云端', paused: '暂停' }[value] || value);
function controlActivity(type, level, text, details = {}) {
  const at = new Date().toISOString();
  const row = { id: `control:${Date.now()}:${Math.random().toString(36).slice(2)}`, at, type, level, exchange: null, symbol: null, message: text, ...details };
  try {
    save('controlActivityLogs', [row, ...get('controlActivityLogs', [])].slice(0, 500));
    recordLog(JSON.stringify({ event: 'execution_control_activity', ...row }));
  } catch (error) { recordLog('切换日志保存失败：' + error.message); }
}
async function loggedChangeMode(target, reason) {
  const from = mode;
  const startedAt = Date.now();
  const details = { from, target, reason, policy };
  controlActivity('execution_switch_started', 'info', `开始切换：${modeName(from)} → ${modeName(target)}；原因：${reason}。`, details);
  try {
    await changeMode(target);
    controlActivity('execution_switch_completed', 'success', `切换完成：当前${modeName(mode)}；耗时 ${Math.round((Date.now() - startedAt) / 1000)} 秒。`, { ...details, currentMode: mode });
  } catch (error) {
    controlActivity('execution_switch_failed', 'error', `切换至${modeName(target)}失败：${error.message}；当前${modeName(mode)}。`, { ...details, currentMode: mode });
    throw error;
  }
}
const cloudCall = (path, method = 'POST', body, timeout = 20_000) => network(cloud + '/control/' + path, {
  method, headers: { authorization: 'Bearer ' + controlToken, 'content-type': 'application/json' },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(timeout),
}).then(responseJson);
const localCall = (path, method = 'POST', body) => mf.dispatchFetch('http://local/admin/' + path, {
  method, headers: { authorization: 'Bearer ' + controlToken, 'content-type': 'application/json' },
  ...(body === undefined ? {} : { body: JSON.stringify(body) }),
}).then(responseJson);
const backup = async (label, payload) => {
  const path = resolve(data, `${label}-${Date.now()}.json`);
  await writeFile(path, JSON.stringify(payload), { flag: 'wx', mode: 0o600 });
  return path;
};
async function grantCloud() {
  quotaGate();
  // Persist BEFORE the external call: an uncertain response is still a lease.
  save('cloudSafeAfter', Date.now() + 220_000);
  await cloudCall('lease', 'POST', { active: true });
}
async function cacheCloudSnapshot() {
  const snapshot = await network(cloud + '/health', { signal: AbortSignal.timeout(10_000) }).then(responseJson);
  if (!Array.isArray(snapshot.announcements) || !Array.isArray(snapshot.events)) throw new Error('云端数据格式异常');
  const replica = get('replica', null) ?? await localCall('export', 'GET');
  save('replica', mergeReplica(replica, snapshot));
  save('replicaAt', new Date().toISOString());
  return snapshot;
}
async function drainCloud() {
  if (Date.now() >= get('cloudSafeAfter', 0)) return;
  try { await cloudCall('lease', 'POST', { active: false }, 8000); } catch { /* absolute lease still expires */ }
  while (Date.now() < get('cloudSafeAfter', 0)) {
    message = `正在等待云端推送退出，剩余 ${Math.ceil((get('cloudSafeAfter', 0) - Date.now()) / 1000)} 秒`;
    await sleep(1000);
  }
}
async function changeMode(target) {
  const owner = get('lastOwner', 'local');
  // Probe the destination before stopping a healthy local collector.
  if (target === 'cloud' && owner !== 'cloud') {
    await probeCloud();
    await cloudCall('stop');
    const remote = await cloudCall('export', 'GET');
    await backup('cloud-before-return', remote);
  }
  mode = 'paused'; save('mode', mode);
  message = '正在停止原运行端并保存记录';
  await localCall('stop');
  if (owner === 'cloud') {
    await drainCloud();
    let exported;
    try {
      await cloudCall('stop');
      exported = await cloudCall('export', 'GET');
    } catch (error) {
      if (isCloudQuota(error)) deferCloudRecovery(error);
      exported = get('replica', null) ?? await localCall('export', 'GET');
      recordLog(JSON.stringify({ event: 'cloud_export_unavailable_using_replica', replicaAt: get('replicaAt', null), error: error.message, at: new Date().toISOString() }));
      message = '云端无法导出，使用本地副本接管；缺口只补查、不补推';
      controlActivity('execution_replica_fallback', 'warning', message);
    }
    await backup('cloud-handoff', exported);
    await localCall('import', 'POST', exported);
    save('lastOwner', 'local');
  }
  if (target === 'local') {
    await drainCloud();
    const exported = await localCall('export', 'GET');
    await localCall('import', 'POST', resumeBaseline(exported));
    await localCall('start');
  } else if (target === 'cloud') {
    const exported = await localCall('export', 'GET');
    await backup('local-handoff', exported);
    save('replica', exported); save('replicaAt', new Date().toISOString());
    await cloudCall('stop');
    await cloudCall('import', 'POST', resumeBaseline(exported), 90_000);
    save('lastOwner', 'cloud');
    await grantCloud();
    let started = false;
    for (let attempt = 0; attempt < 10; attempt++) {
      try { await cloudCall('start'); started = true; break; }
      catch (error) { if (isCloudQuota(error)) throw error; message = '等待云端运行许可生效'; await sleep(8000); }
    }
    if (!started) throw new Error('云端未确认启动；本地保持暂停，请重试切换');
    let healthy = false;
    for (let attempt = 0; attempt < 12; attempt++) {
      try { if (healthySnapshot(await cacheCloudSnapshot())) { healthy = true; break; } } catch (error) { if (isCloudQuota(error)) throw error; }
      await sleep(5000);
    }
    if (!healthy) throw new Error('云端未完成有效扫描，自动退回本地');
    save('lastOwner', 'cloud');
  }
  mode = target; save('mode', mode);
  if (target === 'cloud') save('cloudRecoveryFailures', 0);
  message = target === 'local' ? '本地采集与推送运行；历史只补查不补推' : target === 'cloud' ? '云端运行；本机控制服务保持开启以维持运行许可' : '全部暂停';
}
function beginSwitch(target, reason = '用户手动切换') {
  recordLog(JSON.stringify({ event: 'execution_switch_requested', target, at: new Date().toISOString() }));
  busy = true;
  switchTask = loggedChangeMode(target, reason).catch(async error => {
    message = '切换失败：' + error.message; console.error(message);
    if (policy !== 'auto' && isCloudQuota(error)) deferCloudRecovery(error);
    if (policy === 'auto') {
      deferCloudRecovery(error);
      // Preflight failure never stopped the healthy local owner. Do not restart
      // it or reset its notification baseline just to report a failed probe.
      if (mode === 'local' && get('lastOwner', 'local') === 'local') {
        message = '云端验证未通过，本地持续运行；稍后自动重试';
        return;
      }
      try { await loggedChangeMode('local', '上一次切换失败，自动退回本地'); message = '已退回本地；云端恢复后自动重试'; }
      catch (failure) { message = '自动接管失败：' + failure.message; console.error(message); }
    }
  })
    .finally(() => { busy = false; switchTask = undefined; });
}
async function probeCloud() {
  quotaGate();
  const result = await cloudCall('probe', 'GET', undefined, 45_000);
  if (result.healthy !== true || !Array.isArray(result.checks) || result.checks.length !== 13 ||
      result.checks.some(row => row.ok !== true) || !Number.isFinite(Date.parse(result.checkedAt)) ||
      Math.abs(Date.now() - Date.parse(result.checkedAt)) > 60_000) {
    throw new Error('云端真实抓取未通过：' + (result.checks?.filter(row => !row.ok).map(row => `${row.exchange} ${row.kind}: ${row.error || '不可用'}`).join('；') || '探测数据不完整'));
  }
  if (get('cloudQuotaHold', null)) {
    save('cloudQuotaHold', null);
    controlActivity('execution_quota_recovered', 'success', '云端额度故障后的真实抓取验证已通过；仍需稳定性确认后才切回云端。');
  }
  return result;
}
function deferCloudRecovery(error) {
  if (error?.quotaHeld) return;
  const reason = error?.message || String(error);
  const count = get('cloudRecoveryFailures', 0) + 1;
  save('cloudRecoveryFailures', count);
  const minutes = Math.min(240, 15 * 2 ** Math.min(count - 1, 4));
  nextRecoveryAt = Date.now() + minutes * 60_000;
  if (isCloudQuota(error)) {
    const retryAt = Number.isFinite(error.retryAt) && error.retryAt > 0 ? error.retryAt : null;
    // An explicit future reset takes precedence over exponential retry. Stale
    // reset hints must not cause an immediate retry loop.
    if (retryAt > Date.now()) nextRecoveryAt = retryAt;
    const previous = get('cloudQuotaHold', null);
    const hold = { code: error.code, reason, retryAt, retrySource: error.retrySource || null, detectedAt: previous?.detectedAt || Date.now() };
    save('cloudQuotaHold', hold); save('nextRecoveryAt', nextRecoveryAt);
    if (!previous || previous.code !== hold.code || previous.retryAt !== hold.retryAt || previous.reason !== reason) {
      const timing = retryAt ? `来源给出的${hold.retrySource === 'daily-reset-estimate' ? '预计重置' : '重试'}时间：北京时间 ${beijingTime(retryAt)}` : '来源未提供恢复时间';
      controlActivity('execution_quota_exhausted', 'warning', `云端额度不足，自动模式保留本地接管；${timing}；下次验证 ${beijingTime(nextRecoveryAt)}，到点不直接切回。`, { ...hold, nextRecoveryAt });
    }
    return;
  }
  save('nextRecoveryAt', nextRecoveryAt);
  controlActivity('execution_probe_failed', 'warning', `云端验证失败，保留本地运行；${minutes} 分钟后重试。${reason}`, { nextRecoveryAt });
}
const marks = () => Object.fromEntries(db.prepare('SELECT url,read FROM read_marks').all().map(row => [row.url, Boolean(row.read)]));
const readLocalSnapshot = singleFlightReader(async () => {
  const upstream = await mf.dispatchFetch('http://local/health');
  return { snapshot: await upstream.json(), status: upstream.status };
});
async function bodyJson(request) {
  let body = '';
  for await (const chunk of request) { body += chunk; if (body.length > 8192) throw new Error('请求过大'); }
  return JSON.parse(body);
}
const server = createServer(async (request, response) => {
  const json = (value, code = 200) => { response.writeHead(code, { 'content-type': 'application/json', 'cache-control': 'no-store' }); response.end(JSON.stringify(value)); };
  try {
    if (!['127.0.0.1:' + port, 'localhost:' + port].includes(request.headers.host)) return json({ error: 'invalid_host' }, 403);
    const path = new URL(request.url, `http://${host}:${port}`).pathname;
    if (request.method !== 'GET') {
      const origin = request.headers.origin;
      if (request.headers['x-news-local'] !== '1' || origin && ![`http://127.0.0.1:${port}`, `http://localhost:${port}`].includes(origin)) return json({ error: 'forbidden' }, 403);
    }
    if (path === '/api/control' && request.method === 'GET') return json(status());
    if (path === '/api/control' && request.method === 'POST') {
      if (busy || watchdogBusy) return json({ error: '正在检查或切换，请稍候' }, 409);
      const { mode: target } = await bodyJson(request);
      if (!['auto', 'local', 'cloud', 'paused'].includes(target)) return json({ error: 'invalid_mode' }, 400);
      policy = target === 'auto' ? 'auto' : 'manual'; save('policy', policy);
      failures = 0; recoveryChecks = 0;
      if (target === 'auto' && mode === 'cloud') return json(status());
      beginSwitch(target === 'auto' ? 'cloud' : target); return json(status(), 202);
    }
    if (path === '/api/read-marks') {
      if (request.method === 'POST') {
        const { url, keys = [url], read } = await bodyJson(request);
        if (!Array.isArray(keys) || !keys.length || keys.length > 100 || typeof read !== 'boolean' || !keys.every(key => {
          if (typeof key !== 'string' || key.length > 2048) return false;
          if (key.startsWith('reminder:')) return key.length > 9;
          try { return ['http:', 'https:'].includes(new URL(key).protocol); } catch { return false; }
        })) return json({ error: 'invalid_mark' }, 400);
        db.exec('BEGIN');
        try {
          const statement = db.prepare('INSERT INTO read_marks VALUES(?,?,?) ON CONFLICT(url) DO UPDATE SET read=excluded.read,updated_at=excluded.updated_at WHERE read<>excluded.read');
          for (const key of keys) statement.run(key, Number(read), new Date().toISOString());
          db.exec('COMMIT');
        } catch (error) { db.exec('ROLLBACK'); throw error; }
      } else if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
      return json(marks());
    }
    if (path === '/api/monitor' && request.method === 'GET') {
      if (!mf) return json({ error: '本地监控正在初始化' }, 503);
      const result = mode === 'cloud' ? await (async () => {
        const upstream = await network(cloud + '/health');
        return { snapshot: await upstream.json(), status: upstream.status };
      })() : await readLocalSnapshot();
      const snapshot = structuredClone(result.snapshot);
      snapshot.activityLogs = [...get('controlActivityLogs', []), ...(snapshot.activityLogs || [])]
        .sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).slice(0, 50);
      if (mode === 'local' && !busy && localUnavailable(snapshot)) {
        return json({ error: '本地监控不可用：尚未完成有效扫描，请查看运行状态。', code: 'MONITOR_UNAVAILABLE' }, 503);
      }
      return json(snapshot, result.status);
    }
    if (path.startsWith('/api/')) return json({ error: 'not_found' }, 404);
    if (request.method !== 'GET') return json({ error: 'method_not_allowed' }, 405);
    const publicRoot = resolve(root, '../exchange-news-site/dist-local');
    const asset = resolve(publicRoot, '.' + decodeURIComponent(path === '/' ? '/index.html' : path));
    if (!asset.startsWith(publicRoot + '/')) return json({ error: 'forbidden' }, 403);
    const content = await readFile(asset);
    const type = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml' }[extname(asset)] || 'application/octet-stream';
    response.writeHead(200, { 'content-type': type, 'cache-control': path.startsWith('/assets/') ? 'public,max-age=31536000,immutable' : 'no-store', 'content-security-policy': "frame-ancestors http://127.0.0.1:5173 http://localhost:5173 'self'" }); response.end(content);
  } catch (error) { json({ error: error.code === 'ENOENT' ? 'not_found' : error.message }, error.code === 'ENOENT' ? 404 : 503); }
});
// Bind first: a duplicate launcher must not create a second collector.
await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, host, resolve); });
const options = convertV4MiniflareOptions({
  name: 'exchange-news-local', modules: true, scriptPath: resolve(root, 'dist/index.js'),
  compatibilityDate: '2026-09-02', compatibilityFlags: ['nodejs_compat'],
  kvNamespaces: ['STATE_KV'], durableObjects: { MONITOR: { className: 'ExchangeMonitor', useSQLite: true } },
  bindings: { ADMIN_TOKEN: controlToken, LOCAL_RUNTIME: '1', BARK_WEBHOOK_URL: bark, PUBLIC_SITE_ORIGIN: `http://${host}:5173/exchange-announcements` },
  outboundService: request => forwardOutbound(request, network),
});
options.resourcePersistencePath = resolve(data, 'storage');
mf = new Miniflare(options);
try {
  if (!get('seeded', false)) {
    const original = await cloudCall('backup', 'GET');
    if (original.version !== 1 || !original.inventory || !Array.isArray(original.announcements)) throw new Error('旧备份格式不正确，拒绝空基线启动');
    await backup('original-cloud-20260905', original);
    const seed = structuredClone(original);
    seed.running = false;
    seed.migrationCutoffAt = new Date().toISOString();
    seed.migrationPendingSources = ['bn', 'bg', 'by', 'gate', 'okx', 'aster', 'hl'];
    seed.newsRetryState = {};
    seed.runtime = { lastCycleAt: null, lastContractScanAt: null, lastNewsScanAt: null, nextAlarmAt: null, lastError: null };
    for (const event of seed.events) { event.notificationPolicy = 'historical_only'; if (!event.pushedAt) event.pushStatus = 'suppressed'; }
    const kv = await mf.getKVNamespace('STATE_KV');
    await kv.put('monitor-state-v2', JSON.stringify(seed));
    await mf.dispatchFetch('http://local/health').then(responseJson);
    save('seeded', true); save('lastOwner', 'local');
    save('cloudSafeAfter', Date.now() + 200_000);
    mode = 'local';
  }
  await loggedChangeMode(policy === 'auto' ? 'local' : mode, '控制服务启动，恢复运行状态');
} catch (error) { mode = 'paused'; save('mode', mode); message = '监控不可用：' + error.message; console.error(message); }
busy = false;
const renewal = setInterval(() => {
  if (mode === 'cloud' && !busy && !watchdogBusy) {
    busy = true;
    void grantCloud().catch(error => { message = '云端续期失败：' + error.message; failures = 2; if (isCloudQuota(error)) deferCloudRecovery(error); }).finally(() => { busy = false; });
  }
}, 90_000);
const heartbeat = setInterval(() => {
  if (mode === 'local' && !busy) void localCall('start').catch(error => { message = '本地监控异常：' + error.message; });
}, 60_000);
const watchdog = setInterval(() => {
  if (busy || watchdogBusy) return;
  watchdogBusy = true;
  void (async () => {
    if (mode === 'cloud') {
      try {
        quotaGate();
        const snapshot = await cacheCloudSnapshot();
        failures = healthySnapshot(snapshot) ? 0 : failures + 1;
      } catch (error) {
        if (isCloudQuota(error)) { deferCloudRecovery(error); failures = 2; }
        else failures++;
      }
      if (policy === 'auto' && failures >= 2) {
        if (!get('cloudQuotaHold', null)) { nextRecoveryAt = Date.now() + 10 * 60_000; save('nextRecoveryAt', nextRecoveryAt); }
        recoveryChecks = 0; beginSwitch('local', get('cloudQuotaHold', null) ? '云端额度不足，等待额度恢复期间由本地接管' : '云端连续健康检查失败或运行许可续期失败');
      }
    } else if (policy === 'auto' && Date.now() >= nextRecoveryAt) {
      try {
        await probeCloud();
        nextRecoveryAt = Date.now() + 5 * 60_000; save('nextRecoveryAt', nextRecoveryAt);
        recoveryChecks++;
        if (recoveryChecks >= 2) { recoveryChecks = 0; beginSwitch('cloud', '云端连续两次恢复检查通过'); }
        else message = '本地运行；云端首次恢复，等待下一次稳定性确认';
      } catch (error) { recoveryChecks = 0; deferCloudRecovery(error); message = '本地持续运行；云端抓取未恢复，已延长重试间隔'; }
      if (mode === 'paused' && !busy) beginSwitch('local', '自动模式下监控暂停，恢复本地运行');
    }
  })().catch(error => recordLog('自动巡检异常：' + error.message)).finally(() => { watchdogBusy = false; });
}, 30_000);
// Probe the same HTTP path as the page, rather than trusting an advancing alarm.
// LaunchAgent KeepAlive restarts this process; persisted push locks and baseline
// recovery remain the existing startup path. Limit restarts across processes.
const localStartedAt = Date.now();
let localHealthBusy = false;
let localHealthFailures = 0;
const localHealthTimer = setInterval(() => {
  if (localHealthBusy || busy || mode !== 'local') { localHealthFailures = 0; return; }
  localHealthBusy = true;
  void (async () => {
    try {
      const response = await externalFetch(`http://${host}:${port}/api/monitor`, { signal: AbortSignal.timeout(10_000) });
      const snapshot = await response.json();
      if (!response.ok || localUnavailable(snapshot)) throw new Error(snapshot.error || '本地抓取不可用');
      if (localHealthFailures) controlActivity('local_health_recovered', 'success', '本地网页与抓取健康检查已恢复。');
      localHealthFailures = 0;
    } catch (error) {
      localHealthFailures++;
      if (localHealthFailures === 1) controlActivity('local_health_failed', 'warning', `本地网页或抓取自检失败，等待复核：${error.message}`);
      if (shouldRestartLocal({ failures: localHealthFailures, startedAt: localStartedAt, lastRestartAt: get('localHealthRestartAt', 0) })) {
        save('localHealthRestartAt', Date.now());
        controlActivity('local_health_restart', 'error', '本地新闻连续自检失败，自动重启新闻服务；保留历史与推送去重，不重启 Astro。');
        process.exit(1);
      }
    }
  })().catch(error => recordLog('本地自检异常：' + error.message)).finally(() => { localHealthBusy = false; });
}, 30_000);
async function shutdown() {
  clearInterval(localHealthTimer);
  clearInterval(renewal); clearInterval(heartbeat); clearInterval(watchdog); server.close();
  if (switchTask) await switchTask;
  try { await localCall('stop'); } catch {}
  await mf.dispose(); db.close(); process.exit(0);
}
process.on('SIGTERM', () => void shutdown());
process.on('SIGINT', () => void shutdown());
console.log(JSON.stringify({ event: 'news_local_ready', port, mode }));
