import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/lib/astroRuleDraft.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { rulesFromStatus, changedRuleKeys, formatRuleValue, ruleLabels } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
const status = { spreadScanner: { subscriptions: ['gateFuture', 'bybitFuture'], settingsHealth: { newCardsAllowed: true }, intervalSeconds: 8 } };
test('status fields and change labels have identical coverage', () => {
  assert.deepEqual(Object.keys(rulesFromStatus(status)).sort(), Object.keys(ruleLabels).sort());
});
test('roundtrip has no false modifications', () => {
  const saved = rulesFromStatus(status);
  assert.deepEqual(changedRuleKeys(saved, structuredClone(saved)), []);
});
test('scan interval and Bybit threshold remain independent', () => {
  const saved = rulesFromStatus(status);
  assert.deepEqual(changedRuleKeys(saved, { ...saved, scanIntervalSeconds: 9 }), ['scanIntervalSeconds']);
  assert.deepEqual(changedRuleKeys(saved, { ...saved, ffBybitSellExceptionMinOpenSpreadPct: 11 }), ['ffBybitSellExceptionMinOpenSpreadPct']);
});
test('subscription order is not a change, removal is', () => {
  const saved = rulesFromStatus(status);
  assert.deepEqual(changedRuleKeys(saved, { ...saved, markets: [...saved.markets].reverse() }), []);
  assert.deepEqual(changedRuleKeys(saved, { ...saved, markets: ['gateFuture'] }), ['markets']);
});
test('blocked routes compare by content and retain real changes', () => {
  const saved = { ...rulesFromStatus(status), blockedPairs: [{marketKey:'gateFuture', symbol:'ABCUSDT'}] };
  assert.deepEqual(changedRuleKeys(saved, {...saved, blockedPairs:[{symbol:'ABCUSDT',marketKey:'gateFuture'}]}), []);
  assert.deepEqual(changedRuleKeys(saved, {...saved, blockedPairs:[]}), ['blockedPairs']);
});
test('missing or failed rule reads cannot be used for saving', () => {
  assert.throws(() => rulesFromStatus({}));
  assert.throws(() => rulesFromStatus({spreadScanner:{settingsHealth:{newCardsAllowed:false}}}));
});
test('format values preserves units and disabled states', () => {
  assert.equal(formatRuleValue('scanIntervalSeconds', 8), '8 秒');
  assert.equal(formatRuleValue('ffBybitSellExceptionMinOpenSpreadPct', 10), '10%');
  assert.equal(formatRuleValue('fsBorrowAutoCardEnabled', false), '关闭');
  assert.equal(formatRuleValue('greaterPriceAlertPct', null), '未设置');
});

test('market confirmation uses readable Chinese market names without changing values', () => {
  const markets = ['binanceSpot', 'binanceFuture', 'bybitFuture', 'bitgetSpot', 'okxFuture', 'gateSpot', 'asterFuture', 'okxdexSpot', 'pancakeswapv3Spot'];
  const before = [...markets];
  assert.equal(formatRuleValue('markets', markets), '币安·现货、币安·合约、Bybit·合约、Bitget·现货、欧易·合约、Gate（芝麻开门）·现货、Aster·合约、欧易 DEX·现货、PancakeSwap V3·现货');
  assert.deepEqual(markets, before);
});

test('blocked route names are localized, coin symbols and unknown keys stay intact', () => {
  assert.equal(formatRuleValue('blockedPairs', [{marketKey:'binanceFuture',symbol:'ABCUSDT'}]), '币安·合约：ABCUSDT');
  assert.equal(formatRuleValue('blockedCoins', ['ABC', 'Future']), 'ABC、Future');
  assert.equal(formatRuleValue('markets', ['newExchangeFuture', 'unknown']), 'newExchange·合约、unknown');
  assert.equal(formatRuleValue('markets', []), '无');
});
