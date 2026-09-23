import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { healthySnapshot, mergeReplica, resumeBaseline } from '../failover.mjs';

const source = await readFile(new URL('../local-service.mjs', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('const responseJson ='), source.indexOf('const marks ='));
const now = Date.now();
const seed = () => ({ version: 1, locks: { 'push-lock:old': { status: 'sent' } }, state: { version: 1, running: false, announcements: [], inventory: {}, events: [{ id: 'sent', pushedAt: new Date(now).toISOString() }, { id: 'pending', pushedAt: null, pushStatus: 'pending' }], pushLogs: [], activityLogs: [] } });
const live = () => ({ status: 'degraded', runtime: { lastCycleAt: new Date(now).toISOString() }, sources: [{kind:'contracts',status:'ok',lastCheckedAt:new Date(now).toISOString()},{kind:'news',status:'degraded',lastCheckedAt:new Date(now).toISOString()}], announcements: [{ id: 'new-ann', url: 'https://example.invalid/news', title: '公告', fetchedAt: new Date(now).toISOString() }], events: [], pushLogs: [] });
assert.equal(healthySnapshot(live(), now), true);
assert.equal(healthySnapshot({ ...live(), runtime: { lastCycleAt: '2020-01-01' } }, now), false);
const merged = mergeReplica(seed(), live());
assert.equal(merged.state.announcements[0].key, 'new-ann');
assert.equal(merged.locks['push-lock:old'].status, 'sent');
assert.equal(resumeBaseline(merged, now).state.events.find(row => row.id === 'pending').pushStatus, 'suppressed');
assert.ok(resumeBaseline(merged, now).state.events.find(row => row.id === 'sent').pushedAt);
assert.equal(seed().state.events[1].pushStatus, 'pending');
const limited = seed(); limited.state.newsRetryState = {gate:{failures:5,retryAt:now+900000}};
assert.deepEqual(resumeBaseline(limited,now).state.newsRetryState, {});
assert.equal(limited.state.newsRetryState.gate.failures,5);

async function scenario(owner, failPath, initialSettings = [], reply = () => null) {
  let time = now;
  const settings = new Map([['lastOwner', owner], ['cloudSafeAfter', owner === 'cloud' ? now + 220_000 : 0], ['replica', merged], ...initialSettings]);
  let local = seed();
  let running = owner === 'local';
  const operations = [];
  const network = async (url, options = {}) => {
    const path = new URL(url).pathname;
    operations.push(path);
    const override = reply(path); if (override) return override;
    if (failPath(path)) throw new Error('simulated cloud outage');
    if (path === '/control/probe') return Response.json({healthy:true,checkedAt:new Date(time).toISOString(),checks:Array.from({length:13},()=>({ok:true}))});
    if (path === '/control/export') return Response.json(seed());
    if (path === '/health') return Response.json({ ...live(), runtime: { lastCycleAt: new Date(time).toISOString() }, sources: live().sources.map(row => ({ ...row, lastCheckedAt: new Date(time).toISOString() })) });
    return Response.json({ status: 'ok' });
  };
  const mf = { dispatchFetch: async (url, options = {}) => {
    const path = new URL(url).pathname;
    operations.push('local' + path);
    if (path === '/admin/stop') running = false;
    if (path === '/admin/export') return Response.json(local);
    if (path === '/admin/import') local = JSON.parse(options.body);
    if (path === '/admin/start') { assert.ok(time >= (settings.get('cloudSafeAfter') || 0), 'must drain lease'); running = true; }
    return Response.json({ status: 'ok' });
  } };
  class Clock extends Date { static now() { return time; } }
  const factory = new Function('env', `const {network,mf,get,save,Date,sleep,writeFile,resolve,recordLog,healthySnapshot,mergeReplica,resumeBaseline}=env;
    const cloud='https://cloud.invalid',controlToken='test',data='/test';
    let mode=${JSON.stringify(owner)},policy='auto',busy=false,message='',nextRecoveryAt=get('nextRecoveryAt',0),switchTask;
    ${code}
    return {changeMode,beginSwitch,deferCloudRecovery,probeCloud,responseJson,quotaStatusMessage,done:()=>switchTask,status:()=>({mode,busy,message,nextRecoveryAt})};`);
  const controller = factory({ network, mf, get: (key, fallback) => settings.has(key) ? settings.get(key) : fallback, save: (key,value) => settings.set(key,value), Date: Clock, sleep: async ms => { time += ms; }, writeFile: async()=>{},resolve:(...parts)=>parts.join('/'),recordLog:()=>{},healthySnapshot,mergeReplica,resumeBaseline });
  return { controller, settings, advance:ms=>{time+=ms;}, running:()=>running, local:()=>local, operations, logs:()=>settings.get('controlActivityLogs') || [] };
}
const outage = await scenario('cloud', () => true);
await outage.controller.changeMode('local');
assert.equal(outage.running(), true);
assert.equal(outage.local().state.announcements[0].key, 'new-ann');
assert.equal(outage.local().state.events.find(row=>row.id==='pending').pushStatus, 'suppressed');

const blockedProbe = await scenario('local', path => path === '/control/probe');
blockedProbe.controller.beginSwitch('cloud'); await blockedProbe.controller.done();
assert.equal(blockedProbe.running(), true);
assert.equal(blockedProbe.controller.status().mode, 'local');
assert.equal(blockedProbe.operations.some(path=>path==='local/admin/stop'||path==='/control/lease'),false);
assert.equal(blockedProbe.controller.status().nextRecoveryAt, now+15*60000);
blockedProbe.controller.deferCloudRecovery('still blocked');
assert.equal(blockedProbe.controller.status().nextRecoveryAt, now+30*60000);
for(let i=0;i<5;i++)blockedProbe.controller.deferCloudRecovery('still blocked');
assert.equal(blockedProbe.controller.status().nextRecoveryAt, now+240*60000);

const importFailure = await scenario('local', path => path === '/control/import');
importFailure.controller.beginSwitch('cloud'); await importFailure.controller.done();
assert.equal(importFailure.controller.status().mode, 'local');
assert.equal(importFailure.running(), true);
assert.deepEqual(importFailure.logs().map(row => row.type).reverse(), ['execution_switch_started', 'execution_switch_failed', 'execution_probe_failed', 'execution_switch_started', 'execution_switch_completed']);
assert.equal(importFailure.logs()[0].currentMode, 'local');
assert.match(importFailure.logs()[0].reason, /自动退回/);
assert.ok(importFailure.logs().every(row => row.id && row.at && row.message));

const promotion = await scenario('local', () => false);
await promotion.controller.changeMode('cloud');
assert.equal(promotion.running(), false);
assert.equal(promotion.controller.status().mode, 'cloud');
assert.ok(promotion.operations.indexOf('/control/stop') < promotion.operations.indexOf('local/admin/stop'));
const unhealthyStart = await scenario('local', path => path === '/health');
unhealthyStart.controller.beginSwitch('cloud'); await unhealthyStart.controller.done();
assert.equal(unhealthyStart.controller.status().mode, 'local');
assert.equal(unhealthyStart.running(), true);
console.log('PASS: stale health, replica merge, no historical replay, total cloud outage, failed promotion rollback, healthy promotion, lease drain');

const resetAt = now + 6*3600000;
const quota = await scenario('local',()=>false,[],path=>path==='/control/probe'?Response.json({code:'STORAGE_DAILY_QUOTA_EXCEEDED',error:'每日读取额度耗尽',retryAt:new Date(resetAt).toISOString(),retrySource:'daily-reset-estimate'},{status:503}):null);
quota.controller.beginSwitch('cloud'); await quota.controller.done();
assert.equal(quota.running(),true);
assert.equal(quota.controller.status().nextRecoveryAt,resetAt);
assert.equal(quota.settings.get('cloudQuotaHold').retrySource,'daily-reset-estimate');
assert.match(quota.controller.quotaStatusMessage(),/预计额度重置/);
assert.equal(quota.operations.includes('local/admin/stop'),false);
const quotaCount=quota.logs().filter(r=>r.type==='execution_quota_exhausted').length;
const restarted = await scenario('local',()=>false,[...quota.settings]);
await assert.rejects(restarted.controller.probeCloud(),e=>e.quotaHeld===true);
assert.equal(restarted.operations.length,0,'restart must not retry before persisted reset');
restarted.controller.beginSwitch('cloud');await restarted.controller.done();
assert.equal(restarted.operations.length,0,'manual auto retry must respect quota hold');
assert.equal(restarted.logs().filter(r=>r.type==='execution_quota_exhausted').length,quotaCount);
restarted.advance(6*3600000+1);
assert.match(restarted.controller.quotaStatusMessage(),/尚未确认恢复/);
assert.ok(restarted.settings.get('cloudQuotaHold'),'time alone must not clear quota');
await restarted.controller.probeCloud();
assert.equal(restarted.settings.get('cloudQuotaHold'),null);
assert.equal(restarted.logs().filter(r=>r.type==='execution_quota_recovered').length,1);
await restarted.controller.probeCloud();
assert.equal(restarted.logs().filter(r=>r.type==='execution_quota_recovered').length,1);
assert.equal(restarted.running(),true,'probe success alone must not stop local');

for (const [hint, expected] of [['3600',now+3600000],[new Date(now+3600000).toUTCString(),Math.floor((now+3600000)/1000)*1000]]) {
  const c=await scenario('local',()=>false);
  const e=await c.controller.responseJson(Response.json({code:'CLOUD_QUOTA_EXCEEDED',error:'云端额度不足'},{status:503,headers:{'retry-after':hint}})).catch(e=>e);
  c.controller.deferCloudRecovery(e);
  assert.equal(c.controller.status().nextRecoveryAt,expected);
  assert.equal(c.settings.get('cloudQuotaHold').retrySource,'retry-after');
}
for (const status of [403,429,503]) {
  const c=await scenario('local',()=>false);
  const e=await c.controller.responseJson(Response.json({error:'Gate 请求限流 / quota exceeded'},{status,headers:{'retry-after':'86400'}})).catch(e=>e);
  c.controller.deferCloudRecovery(e);
  assert.equal(c.settings.get('cloudQuotaHold'),undefined,'HTTP status alone is not platform quota');
  assert.equal(c.controller.status().nextRecoveryAt,now+15*60000);
}
const missing=await scenario('local',()=>false);
missing.controller.deferCloudRecovery(Object.assign(new Error('额度不足'),{code:'CLOUD_QUOTA_EXCEEDED',retryAt:null}));
assert.match(missing.controller.quotaStatusMessage(),/未提供额度恢复时间/);
assert.equal(missing.controller.status().nextRecoveryAt,now+15*60000);
const expired=await scenario('local',()=>false);
expired.controller.deferCloudRecovery(Object.assign(new Error('额度仍不足'),{code:'CLOUD_QUOTA_EXCEEDED',retryAt:now-1000}));
assert.equal(expired.controller.status().nextRecoveryAt,now+15*60000,'stale reset hint must not busy retry');
console.log('PASS: quota reset persistence, no early network retry, recovery validation, log dedup, Retry-After and exchange-limit separation');
