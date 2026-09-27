import type { CryptoFsSignalsResponse } from "../api";

type Status = NonNullable<CryptoFsSignalsResponse["astroAutoCard"]>;

export function rulesFromStatus(status: Status) {
  const s = status.spreadScanner;
  if (!s || s.settingsHealth?.newCardsAllowed === false) throw new Error("未取得有效的已保存规则");
  return {
    markets: [...s.subscriptions], minVolumeUsdt: s.minVolumeUsdt ?? 10000,
    blockedPairs: [...(s.blockedPairs ?? [])], blockedCoins: [...(s.blockedCoins ?? [])],
    deleteRearmPct: s.deleteRearmPct ?? 20, deletePullbackPctPoints: s.deletePullbackPctPoints ?? 0.5,
    ffMinOpenSpreadPct: s.autoCardRules?.ff.minOpenSpreadPctExclusive ?? 1,
    ffBybitSellExceptionEnabled: s.autoCardRules?.ff.bybitSellException?.enabled ?? false,
    ffBybitSellExceptionMinOpenSpreadPct: s.autoCardRules?.ff.bybitSellException?.minOpenSpreadPctExclusive ?? 10,
    scanIntervalSeconds: s.intervalSeconds ?? 8,
    sfMinOpenSpreadPct: s.autoCardRules?.sf.minOpenSpreadPctExclusive ?? 1.3,
    sfOkxdexMinOpenSpreadPct: s.autoCardRules?.sf.dexMinOpenSpreadPctExclusive?.okxdex ?? 1.5,
    sfPancakeswapV3MinOpenSpreadPct: s.autoCardRules?.sf.dexMinOpenSpreadPctExclusive?.pancakeswapv3 ?? 1.5,
    sfMinShortFundingRatePct: s.autoCardRules?.sf.minShortFundingRatePct ?? 0,
    sfOkxdexAutoCardEnabled: s.autoCardRules?.sf.okxDexRoute?.autoCardEnabled ?? true,
    sfPancakeswapV3AutoCardEnabled: s.autoCardRules?.sf.pancakeswapV3Enabled ?? false,
    fsBorrowAutoCardEnabled: s.autoCardRules?.fsBorrow?.enabled ?? true,
    fsBorrowMinCycleProfitPct: s.autoCardRules?.fsBorrow?.minCycleProfitPctExclusive ?? 0.2,
    fsBorrowMinOpenSpreadPct: s.autoCardRules?.fsBorrow?.minOpenSpreadPctExclusive ?? 1,
    confirmations: s.confirmations ?? 2, maxQuoteAgeSeconds: s.maxQuoteAgeSeconds ?? 20,
    excludeDelistedExchangeCards: s.delistingRule?.enabled ?? true,
    greaterPriceAlertPct: status.defaultGreaterPriceAlertPct ?? null,
    priceChangeAlertPct: status.defaultPriceChangeAlertPct ?? null,
    priceChangeAlertOnlyRise: status.defaultPriceChangeAlertOnlyRise ?? false,
    minNotionalUsdt: status.defaultMinNotionalUsdt ?? 6, maxNotionalUsdt: status.defaultMaxNotionalUsdt ?? 40
  };
}

export type RuleValues = ReturnType<typeof rulesFromStatus>;
export const ruleLabels: Record<keyof RuleValues, string> = {
  markets: "行情源", minVolumeUsdt: "成交额门槛", blockedPairs: "定向屏蔽", blockedCoins: "全局屏蔽币种",
  deleteRearmPct: "未回弱直接突破", deletePullbackPctPoints: "删除后有效回弱",
  ffMinOpenSpreadPct: "FF 差价", ffBybitSellExceptionEnabled: "Bybit 卖出腿例外开关",
  ffBybitSellExceptionMinOpenSpreadPct: "Bybit 例外差价", scanIntervalSeconds: "全量发现间隔",
  sfMinOpenSpreadPct: "CEX SF 差价", sfOkxdexMinOpenSpreadPct: "OKXDEX 差价",
  sfPancakeswapV3MinOpenSpreadPct: "PancakeSwap V3 差价", sfMinShortFundingRatePct: "SF 最低资金费",
  sfOkxdexAutoCardEnabled: "OKXDEX 开关", sfPancakeswapV3AutoCardEnabled: "PancakeSwap V3 开关",
  fsBorrowAutoCardEnabled: "FS 开关", fsBorrowMinCycleProfitPct: "FS 周期净收益",
  fsBorrowMinOpenSpreadPct: "FS 差价", confirmations: "确认次数", maxQuoteAgeSeconds: "报价有效期",
  excludeDelistedExchangeCards: "旧公告下架排除", greaterPriceAlertPct: "差价报警",
  priceChangeAlertPct: "涨跌幅报警", priceChangeAlertOnlyRise: "仅上涨报警",
  minNotionalUsdt: "最小单笔金额", maxNotionalUsdt: "最大单笔金额"
};

function canonical(value: unknown): string {
  if (Array.isArray(value)) return JSON.stringify(value.map(canonical).sort());
  if (value && typeof value === "object") return JSON.stringify(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)));
  return JSON.stringify(value);
}

export function changedRuleKeys(before: RuleValues, after: RuleValues) {
  return (Object.keys(ruleLabels) as (keyof RuleValues)[]).filter(key => canonical(before[key]) !== canonical(after[key]));
}

const marketExchangeNames: Record<string, string> = {
  binance: "币安", bybit: "Bybit", bitget: "Bitget", okx: "欧易", gate: "Gate（芝麻开门）",
  aster: "Aster", kucoin: "库币", hl: "Hyperliquid", bp: "Backpack", lighter: "Lighter",
  mexc: "抹茶", okxdex: "欧易 DEX", jup: "Jupiter", pancakeswapv3: "PancakeSwap V3", uniswapv3: "Uniswap V3"
};

function formatMarketName(key: string): string {
  const match = /^(.*)(Spot|Future)$/i.exec(key);
  if (!match) return key;
  const exchange = marketExchangeNames[match[1].toLowerCase()] ?? match[1];
  return `${exchange}·${match[2].toLowerCase() === "spot" ? "现货" : "合约"}`;
}

export function formatRuleValue(key: keyof RuleValues, value: RuleValues[keyof RuleValues]): string {
  if (value == null) return "未设置";
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (Array.isArray(value)) return value.map(item => typeof item === "string"
    ? (key === "markets" ? formatMarketName(item) : item)
    : `${formatMarketName(item.marketKey)}：${item.symbol}`).join("、") || "无";
  const unit = key.endsWith("Seconds") ? " 秒" : key.endsWith("Usdt") ? " USDT" : key.endsWith("PctPoints") ? " 个百分点" : key.endsWith("Pct") ? "%" : "";
  return `${value}${unit}`;
}
