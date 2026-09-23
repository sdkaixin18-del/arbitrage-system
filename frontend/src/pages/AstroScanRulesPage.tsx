import {
  DeleteOutlined,
  EditOutlined,
  ReloadOutlined,
  SaveOutlined,
  SearchOutlined,
  SettingOutlined
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";
import type {
  CryptoExchange,
  CryptoMarketType,
  CryptoSymbolMapping,
  CryptoSymbolMappingScanResponse
} from "../api";
import { cryptoApi as api } from "../api/crypto";
import { useAstroRulesDraft } from "./AstroAutoCardLayout";

interface BlockedPair {
  marketKey: string;
  symbol: string;
}

type BlockMarketType = "spot" | "future";

interface MappingDraft {
  exchange: CryptoExchange | null;
  marketType: CryptoMarketType;
  inputSymbol: string;
  mappedSymbol: string;
  priceRatio: number;
  note: string;
}

const emptyMapping = (): MappingDraft => ({
  exchange: null,
  marketType: "futures",
  inputSymbol: "",
  mappedSymbol: "",
  priceRatio: 1,
  note: ""
});

const marketTypeOptions = [
  { value: "spot", label: "现货" },
  { value: "futures", label: "合约" }
];

const priceRatioOptions = [
  { value: 1e-9, label: "1/B (10^-9)" },
  { value: 1e-6, label: "1/M (10^-6)" },
  { value: 0.001, label: "1/K (0.001)" },
  { value: 1, label: "1" },
  { value: 1000, label: "K (1000)" },
  { value: 1e6, label: "M (10^6)" },
  { value: 1e9, label: "B (10^9)" }
];

export default function AstroScanRulesPage() {
  const location = useLocation();
  const draftRef = useAstroRulesDraft();
  const readDraft = <T,>(key: string, fallback: T): T => draftRef.current && key in draftRef.current
    ? draftRef.current[key] as T : fallback;
  const queryClient = useQueryClient();
  const [draftLoaded, setDraftLoaded] = useState(Boolean(draftRef.current));
  const [markets, setMarkets] = useState<string[]>(() => readDraft("markets", []));
  const [minVolumeUsdt, setMinVolumeUsdt] = useState(() => readDraft("minVolumeUsdt", 10000));
  const [deleteRearmPct, setDeleteRearmPct] = useState(() => readDraft("deleteRearmPct", 20));
  const [deletePullbackPctPoints, setDeletePullbackPctPoints] = useState(() => readDraft("deletePullbackPctPoints", 0.5));
  const [ffBybitSellExceptionEnabled, setFfBybitSellExceptionEnabled] = useState(() => readDraft("ffBybitSellExceptionEnabled", false));
  const [ffMinOpenSpreadPct, setFfMinOpenSpreadPct] = useState(() => readDraft("ffMinOpenSpreadPct", 1));
  const [sfMinOpenSpreadPct, setSfMinOpenSpreadPct] = useState(() => readDraft("sfMinOpenSpreadPct", 1.3));
  const [sfOkxdexMinOpenSpreadPct, setSfOkxdexMinOpenSpreadPct] = useState(() => readDraft("sfOkxdexMinOpenSpreadPct", 1.5));
  const [sfPancakeswapV3MinOpenSpreadPct, setSfPancakeswapV3MinOpenSpreadPct] = useState(() => readDraft("sfPancakeswapV3MinOpenSpreadPct", 1.5));
  const [sfMinShortFundingRatePct, setSfMinShortFundingRatePct] = useState(() => readDraft("sfMinShortFundingRatePct", 0));
  const [sfPancakeswapV3AutoCardEnabled, setSfPancakeswapV3AutoCardEnabled] = useState(() => readDraft("sfPancakeswapV3AutoCardEnabled", false));
  const [sfOkxdexAutoCardEnabled, setSfOkxdexAutoCardEnabled] = useState(() => readDraft("sfOkxdexAutoCardEnabled", true));
  const [fsBorrowAutoCardEnabled, setFsBorrowAutoCardEnabled] = useState(() => readDraft("fsBorrowAutoCardEnabled", true));
  const [fsBorrowMinCycleProfitPct, setFsBorrowMinCycleProfitPct] = useState(() => readDraft("fsBorrowMinCycleProfitPct", 0.2));
  const [fsBorrowMinOpenSpreadPct, setFsBorrowMinOpenSpreadPct] = useState(() => readDraft("fsBorrowMinOpenSpreadPct", 1));
  const [confirmations, setConfirmations] = useState(() => readDraft("confirmations", 2));
  const [maxQuoteAgeSeconds, setMaxQuoteAgeSeconds] = useState(() => readDraft("maxQuoteAgeSeconds", 20));
  const [excludeDelistedExchangeCards, setExcludeDelistedExchangeCards] = useState(() => readDraft("excludeDelistedExchangeCards", true));
  const [greaterPriceAlertPct, setGreaterPriceAlertPct] = useState<number | null>(() => readDraft("greaterPriceAlertPct", 2));
  const [priceChangeAlertPct, setPriceChangeAlertPct] = useState<number | null>(() => readDraft("priceChangeAlertPct", null));
  const [priceChangeAlertOnlyRise, setPriceChangeAlertOnlyRise] = useState(() => readDraft("priceChangeAlertOnlyRise", false));
  const [minNotionalUsdt, setMinNotionalUsdt] = useState(() => readDraft("minNotionalUsdt", 6));
  const [maxNotionalUsdt, setMaxNotionalUsdt] = useState(() => readDraft("maxNotionalUsdt", 40));
  const [blockedCoins, setBlockedCoins] = useState<string[]>(() => readDraft("blockedCoins", []));
  const [blockedCoinInput, setBlockedCoinInput] = useState(() => readDraft("blockedCoinInput", ""));
  const [blockedPairs, setBlockedPairs] = useState<BlockedPair[]>(() => readDraft("blockedPairs", []));
  const [blockExchanges, setBlockExchanges] = useState<string[]>(() => readDraft("blockExchanges", []));
  const [blockMarketTypes, setBlockMarketTypes] = useState<BlockMarketType[]>(() => readDraft("blockMarketTypes", ["spot", "future"]));
  const [blockSymbol, setBlockSymbol] = useState(() => readDraft("blockSymbol", ""));
  const [blockedSearch, setBlockedSearch] = useState(() => readDraft("blockedSearch", ""));
  const [mappingDraft, setMappingDraft] = useState<MappingDraft>(() => readDraft("mappingDraft", emptyMapping()));
  const [editingMappingId, setEditingMappingId] = useState<number | null>(() => readDraft("editingMappingId", null));
  const [mappingScanResult, setMappingScanResult] = useState<CryptoSymbolMappingScanResponse | null>(() => readDraft("mappingScanResult", null));

  const statusQuery = useQuery({
    queryKey: ["astro-auto-card-status"],
    queryFn: () => api.astroAutoCardStatus(),
    refetchInterval: 2000,
    retry: false
  });
  const settingsQuery = useQuery({
    queryKey: ["fs-settings"],
    queryFn: () => api.fsSettings()
  });
  const scanner = statusQuery.data?.spreadScanner;

  const loadRules = (nextScanner = scanner, nextStatus = statusQuery.data) => {
    if (!nextScanner) return;
    setSfPancakeswapV3AutoCardEnabled(nextScanner.autoCardRules?.sf.pancakeswapV3Enabled ?? false);
    setMarkets([...nextScanner.subscriptions]);
    setMinVolumeUsdt(nextScanner.minVolumeUsdt ?? 10000);
    setDeleteRearmPct(nextScanner.deleteRearmPct ?? 20);
    setDeletePullbackPctPoints(nextScanner.deletePullbackPctPoints ?? 0.5);
    setFfBybitSellExceptionEnabled(nextScanner.autoCardRules?.ff.bybitSellException?.enabled ?? false);
    setFfMinOpenSpreadPct(nextScanner.autoCardRules?.ff.minOpenSpreadPctExclusive ?? 1);
    setSfMinOpenSpreadPct(nextScanner.autoCardRules?.sf.minOpenSpreadPctExclusive ?? 1.3);
    setSfOkxdexMinOpenSpreadPct(nextScanner.autoCardRules?.sf.dexMinOpenSpreadPctExclusive?.okxdex ?? 1.5);
    setSfPancakeswapV3MinOpenSpreadPct(nextScanner.autoCardRules?.sf.dexMinOpenSpreadPctExclusive?.pancakeswapv3 ?? 1.5);
    setSfMinShortFundingRatePct(nextScanner.autoCardRules?.sf.minShortFundingRatePct ?? 0);
    setSfOkxdexAutoCardEnabled(nextScanner.autoCardRules?.sf.okxDexRoute?.autoCardEnabled ?? true);
    setFsBorrowAutoCardEnabled(nextScanner.autoCardRules?.fsBorrow?.enabled ?? true);
    setFsBorrowMinCycleProfitPct(nextScanner.autoCardRules?.fsBorrow?.minCycleProfitPctExclusive ?? 0.2);
    setFsBorrowMinOpenSpreadPct(nextScanner.autoCardRules?.fsBorrow?.minOpenSpreadPctExclusive ?? 1);
    setConfirmations(nextScanner.confirmations ?? 2);
    setMaxQuoteAgeSeconds(nextScanner.maxQuoteAgeSeconds ?? 20);
    setExcludeDelistedExchangeCards(nextScanner.delistingRule?.enabled ?? true);
    setGreaterPriceAlertPct(nextStatus?.defaultGreaterPriceAlertPct ?? null);
    setPriceChangeAlertPct(nextStatus?.defaultPriceChangeAlertPct ?? null);
    setPriceChangeAlertOnlyRise(nextStatus?.defaultPriceChangeAlertOnlyRise ?? false);
    setMinNotionalUsdt(nextStatus?.defaultMinNotionalUsdt ?? 6);
    setMaxNotionalUsdt(nextStatus?.defaultMaxNotionalUsdt ?? 40);
    setBlockedCoins([...(nextScanner.blockedCoins ?? [])]);
    setBlockedPairs([...(nextScanner.blockedPairs ?? [])]);
    setDraftLoaded(true);
  };

  useEffect(() => {
    if (scanner && !draftLoaded) loadRules();
  }, [scanner, draftLoaded]);

  useEffect(() => {
    if (!location.hash || !draftLoaded) return;
    const timer = window.setTimeout(() => {
      document.getElementById(location.hash.slice(1))?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 120);
    return () => window.clearTimeout(timer);
  }, [location.hash, draftLoaded]);

  useEffect(() => {
    if (draftLoaded) draftRef.current = {
      markets, minVolumeUsdt, deleteRearmPct, deletePullbackPctPoints, ffMinOpenSpreadPct, ffBybitSellExceptionEnabled, sfMinOpenSpreadPct, sfOkxdexMinOpenSpreadPct, sfPancakeswapV3MinOpenSpreadPct, sfMinShortFundingRatePct, sfOkxdexAutoCardEnabled, sfPancakeswapV3AutoCardEnabled, fsBorrowAutoCardEnabled, fsBorrowMinCycleProfitPct, fsBorrowMinOpenSpreadPct, confirmations, maxQuoteAgeSeconds, excludeDelistedExchangeCards, greaterPriceAlertPct, priceChangeAlertPct, priceChangeAlertOnlyRise, minNotionalUsdt, maxNotionalUsdt, blockedCoins, blockedCoinInput, blockedPairs, blockExchanges, blockMarketTypes, blockSymbol, blockedSearch, mappingDraft, editingMappingId, mappingScanResult
    };
  }, [draftLoaded, draftRef, markets, minVolumeUsdt, deleteRearmPct, deletePullbackPctPoints, ffMinOpenSpreadPct, ffBybitSellExceptionEnabled, sfMinOpenSpreadPct, sfOkxdexMinOpenSpreadPct, sfPancakeswapV3MinOpenSpreadPct, sfMinShortFundingRatePct, sfOkxdexAutoCardEnabled, sfPancakeswapV3AutoCardEnabled, fsBorrowAutoCardEnabled, fsBorrowMinCycleProfitPct, fsBorrowMinOpenSpreadPct, confirmations, maxQuoteAgeSeconds, excludeDelistedExchangeCards, greaterPriceAlertPct, priceChangeAlertPct, priceChangeAlertOnlyRise, minNotionalUsdt, maxNotionalUsdt, blockedCoins, blockedCoinInput, blockedPairs, blockExchanges, blockMarketTypes, blockSymbol, blockedSearch, mappingDraft, editingMappingId, mappingScanResult]);

  const reloadRules = async () => {
    const result = await statusQuery.refetch();
    if (result.data?.spreadScanner) loadRules(result.data.spreadScanner, result.data);
    void settingsQuery.refetch();
  };

  const marketsByExchange = useMemo(() => {
    const grouped = new Map<string, NonNullable<typeof scanner>["availableMarkets"]>();
    for (const market of scanner?.availableMarkets ?? []) {
      grouped.set(market.exchangeName, [...(grouped.get(market.exchangeName) ?? []), market]);
    }
    return Array.from(grouped.entries());
  }, [scanner?.availableMarkets]);

  const blockExchangeOptions = useMemo(() => {
    const options = new Map<string, string>();
    for (const market of scanner?.availableMarkets ?? []) options.set(market.exchange, market.exchangeName);
    return Array.from(options, ([value, label]) => ({ value, label }));
  }, [scanner?.availableMarkets]);

  const marketLabel = (marketKey: string) => {
    const market = scanner?.availableMarkets.find((item) => item.key === marketKey);
    return market
      ? `${market.exchangeName} ${market.marketType === "spot" ? "现货" : "合约"}`
      : marketKey;
  };

  const blockedSearchText = blockedSearch.trim().toUpperCase();
  const visibleBlockedPairs = blockedSearchText
    ? blockedPairs.filter((item) => (
      item.symbol.toUpperCase().includes(blockedSearchText)
      || marketLabel(item.marketKey).toUpperCase().includes(blockedSearchText)
    ))
    : blockedPairs;

  const saveRules = useMutation({
    mutationFn: () => {
      return api.updateAstroSpreadSubscriptions({
      markets,
      minVolumeUsdt,
      blockedPairs,
      blockedCoins,
      deleteRearmPct,
      deletePullbackPctPoints,
      ffMinOpenSpreadPct,
      ffBybitSellExceptionEnabled,
      sfMinOpenSpreadPct,
      sfOkxdexMinOpenSpreadPct,
      sfPancakeswapV3MinOpenSpreadPct,
      sfMinShortFundingRatePct,
      sfOkxdexAutoCardEnabled,
      sfPancakeswapV3AutoCardEnabled,
      fsBorrowAutoCardEnabled,
      fsBorrowMinCycleProfitPct,
      fsBorrowMinOpenSpreadPct,
      confirmations,
      maxQuoteAgeSeconds,
      excludeDelistedExchangeCards,
      greaterPriceAlertPct,
      priceChangeAlertPct,
      priceChangeAlertOnlyRise,
      minNotionalUsdt,
      maxNotionalUsdt
    });
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["astro-auto-card-status"], data);
      message.success(`扫描规则已保存，下一轮 ${data.spreadScanner?.intervalSeconds ?? 5} 秒扫描生效`);
    },
    onError: (error) => { message.error(`保存失败：${String(error)}`); }
  });


  const scanMappings = useMutation({
    mutationFn: () => api.scanFsSymbolMappings(),
    onSuccess: async (data) => {
      setMappingScanResult(data);
      await settingsQuery.refetch();
      message.success(`映射扫描完成：新增 ${data.createdCount}，更新 ${data.updatedCount}`);
    },
    onError: (error) => message.error(`映射扫描失败：${String(error)}`)
  });

  const toggleMarket = (key: string, checked: boolean) => {
    setMarkets((current) => checked
      ? Array.from(new Set([...current, key]))
      : current.filter((item) => item !== key));
  };

  const addBlockedCoin = () => {
    const symbol = blockedCoinInput.trim().toUpperCase().replace(/USDT$/, "");
    if (!symbol || !/^[A-Z0-9]+$/.test(symbol)) {
      message.warning("请输入正确币种，例如 VANRY");
      return;
    }
    setBlockedCoins((current) => Array.from(new Set([...current, symbol])));
    setBlockedCoinInput("");
  };

  const addBlockedPair = () => {
    const symbol = blockSymbol.trim().toUpperCase().replace(/USDT$/, "");
    if (!blockExchanges.length || !blockMarketTypes.length || !symbol || !/^[A-Z0-9]+$/.test(symbol)) {
      message.warning("请先选择交易所和市场，再输入正确币种");
      return;
    }
    const normalized = `${symbol}USDT`;
    const nextRules = (scanner?.availableMarkets ?? [])
      .filter((market) => blockExchanges.includes(market.exchange) && blockMarketTypes.includes(market.marketType))
      .map((market) => ({ marketKey: market.key, symbol: normalized }));
    setBlockedPairs((current) => {
      const known = new Set(current.map((item) => `${item.marketKey}:${item.symbol}`));
      return [...current, ...nextRules.filter((item) => !known.has(`${item.marketKey}:${item.symbol}`))];
    });
    setBlockSymbol("");
  };

  const resetMapping = () => {
    setEditingMappingId(null);
    setMappingDraft(emptyMapping());
  };

  const editMapping = (mapping: CryptoSymbolMapping) => {
    setEditingMappingId(mapping.id);
    setMappingDraft({
      exchange: mapping.exchange,
      marketType: mapping.marketType,
      inputSymbol: mapping.inputSymbol,
      mappedSymbol: mapping.mappedSymbol,
      priceRatio: mapping.priceRatio,
      note: mapping.note ?? ""
    });
  };

  const saveMapping = async () => {
    if (!mappingDraft.exchange || !mappingDraft.inputSymbol.trim() || !mappingDraft.mappedSymbol.trim()) {
      message.warning("请填写统一币名、交易所和该交易所实际币名");
      return;
    }
    const payload = {
      inputSymbol: mappingDraft.inputSymbol.trim().toUpperCase(),
      exchange: mappingDraft.exchange,
      marketType: mappingDraft.marketType,
      mappedSymbol: mappingDraft.mappedSymbol.trim().toUpperCase(),
      priceRatio: mappingDraft.priceRatio,
      note: mappingDraft.note.trim() || null
    };
    try {
      if (editingMappingId) await api.updateFsSymbolMapping(editingMappingId, payload);
      else await api.createFsSymbolMapping(payload);
      resetMapping();
      await settingsQuery.refetch();
      message.success("币名映射已保存，下一轮差价扫描生效");
    } catch (error) {
      message.error(`映射保存失败：${String(error)}`);
    }
  };

  const deleteMapping = async (mapping: CryptoSymbolMapping) => {
    try {
      await api.deleteFsSymbolMapping(mapping.id);
      if (editingMappingId === mapping.id) resetMapping();
      await settingsQuery.refetch();
      message.success("映射已删除");
    } catch (error) {
      message.error(`删除失败：${String(error)}`);
    }
  };

  const mappingColumns: ColumnsType<CryptoSymbolMapping> = [
    { title: "统一币名", dataIndex: "inputSymbol", width: 105, render: (value) => <strong>{value}</strong> },
    {
      title: "交易所 / 市场",
      width: 145,
      render: (_, row) => `${settingsQuery.data?.availableExchanges.find((item) => item.code === row.exchange)?.name ?? row.exchange} · ${row.marketType === "spot" ? "现货" : "合约"}`
    },
    { title: "交易所实际币名", dataIndex: "mappedSymbol", width: 145, render: (value) => <Tag color="blue">{value}</Tag> },
    { title: "价格倍率", dataIndex: "priceRatio", width: 95 },
    { title: "备注", dataIndex: "note", ellipsis: true, render: (value) => value || "-" },
    {
      title: "操作",
      width: 95,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" shape="circle" icon={<EditOutlined />} onClick={() => editMapping(row)} />
          <Popconfirm title="删除这条映射？" onConfirm={() => deleteMapping(row)}>
            <Button size="small" shape="circle" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      )
    }
  ];


  return (
    <div className="astro-rules-page">
      <header className="astro-page-toolbar">
        <div>
          <Typography.Title level={3}>套利规则</Typography.Title>
          <Typography.Text type="secondary">设置建卡门槛和新卡参数；链上配置请前往“DEX 配置”。</Typography.Text>
        </div>
        <Space wrap>
          <Button icon={<ReloadOutlined />} loading={statusQuery.isFetching} onClick={reloadRules}>重新载入</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saveRules.isPending} disabled={!draftLoaded || markets.length < 2} onClick={() => saveRules.mutate()}>保存全部规则</Button>
        </Space>
      </header>

      {statusQuery.isError ? <Alert type="error" showIcon message="规则读取失败" description={String(statusQuery.error)} /> : null}


      <section className="astro-rules-card astro-auto-rule-card">
        <div className="astro-rules-section-head">
          <div>
            <strong>发现门槛与新卡默认参数</strong>{" "}
            <Typography.Text type="secondary">筛选候选并复核真实盘口；保存后生效</Typography.Text>
          </div>
          <Button type="primary" size="small" icon={<SaveOutlined />} loading={saveRules.isPending} disabled={!draftLoaded || markets.length < 2} onClick={() => saveRules.mutate()}>
            保存全部规则
          </Button>
        </div>
        <div className="astro-auto-rule-list">
          <div className="astro-auto-rule-row">
            <div className="astro-auto-rule-index">01</div>
            <div className="astro-auto-rule-copy">
              <strong>FF 合约—合约</strong>
              <span>允许 BN / BG / OKX / Gate / Aster；Bybit 默认只作买入腿；可开启大差价卖出腿例外；不创建 GC 卡</span>
            </div>
            <label><span>发现差价大于</span><InputNumber min={0.01} max={100} step={0.1} precision={2} value={ffMinOpenSpreadPct} onChange={(value) => setFfMinOpenSpreadPct(Number(value ?? 1))} addonAfter="%" /></label>
          </div>
          <div className="astro-auto-rule-row">
            <div className="astro-auto-rule-index">01+</div>
            <div className="astro-auto-rule-copy">
              <strong>Bybit 卖出腿例外</strong>
              <span>其他交易所买入 / Bybit 卖出的 FF，实际开仓差价 &gt; 10% 才建卡；仍须双轮真实盘口复核，新卡暂停</span>
            </div>
            <label><span>允许大于 10% 的例外</span><Switch checked={ffBybitSellExceptionEnabled} onChange={setFfBybitSellExceptionEnabled} /></label>
          </div>
          <div className="astro-auto-rule-row">
            <div className="astro-auto-rule-index">02</div>
            <div className="astro-auto-rule-copy">
              <strong>SF 现货—合约</strong>
              <span>Pulse 初筛；盘口条件通过后查精确资金费 ≥ 0，缓存 10 秒。实际价差 ≥ 2.5% 豁免</span>
            </div>
            <label><span>CEX 现货差价大于</span><InputNumber min={0.01} max={100} step={0.1} precision={2} value={sfMinOpenSpreadPct} onChange={(value) => setSfMinOpenSpreadPct(Number(value ?? 1.3))} addonAfter="%" /></label>
            <div className="astro-dex-switches">
            <Typography.Text type="secondary">两家 DEX 分别设置；发现和实际可成交价差均须严格超过对应门槛，等于不建卡。链上–交易所建卡前连续 3 次真实询价，每轮间隔 1 秒并核对同数量合约深度；不复用热点报价，重复或过期报价、任一轮不达标均停止本轮建卡。</Typography.Text>
            <div className="astro-auto-rule-switch">
              <Switch checked={sfOkxdexAutoCardEnabled} onChange={setSfOkxdexAutoCardEnabled} />
              <b>OKXDEX {sfOkxdexAutoCardEnabled ? "开卡启用" : "开卡暂停"}</b>
              <label><span>OKXDEX 差价大于</span><InputNumber aria-label="OKXDEX 建卡差价门槛" min={0.01} max={100} step={0.1} precision={2} value={sfOkxdexMinOpenSpreadPct} onChange={value => setSfOkxdexMinOpenSpreadPct(Number(value ?? 1.5))} addonAfter="%" /></label>
            </div>
            <div className="astro-auto-rule-switch">
              <Switch checked={sfPancakeswapV3AutoCardEnabled} onChange={setSfPancakeswapV3AutoCardEnabled} />
              <b>PancakeSwap V3 {sfPancakeswapV3AutoCardEnabled ? "开卡启用" : "开卡暂停"}</b>
              <label><span>PancakeSwap V3 差价大于</span><InputNumber aria-label="PancakeSwap V3 建卡差价门槛" min={0.01} max={100} step={0.1} precision={2} value={sfPancakeswapV3MinOpenSpreadPct} onChange={value => setSfPancakeswapV3MinOpenSpreadPct(Number(value ?? 1.5))} addonAfter="%" /></label>
            </div>
            </div>
          </div>
          <div className="astro-auto-rule-row astro-auto-fs-rule-row">
            <div className="astro-auto-rule-index">03</div>
            <div className="astro-auto-rule-copy">
              <strong>FS 借币现货—合约</strong>
              <span>任意已接入合约 / BG 全仓杠杆现货；按同一资金费周期扣除借币成本；BG 实时可借额度必须大于 0</span>
            </div>
            <div className="astro-auto-rule-switch">
              <Switch checked={fsBorrowAutoCardEnabled} onChange={setFsBorrowAutoCardEnabled} />
              <b>{fsBorrowAutoCardEnabled ? "已启用" : "已关闭"}</b>
            </div>
            <label><span>周期净收益大于</span><InputNumber min={0} max={100} step={0.05} precision={3} value={fsBorrowMinCycleProfitPct} onChange={(value) => setFsBorrowMinCycleProfitPct(Number(value ?? 0.2))} addonAfter="%" /></label>
            <label><span>发现差价大于</span><InputNumber min={0.01} max={100} step={0.1} precision={2} value={fsBorrowMinOpenSpreadPct} onChange={(value) => setFsBorrowMinOpenSpreadPct(Number(value ?? 1))} addonAfter="%" /></label>
          </div>
          <div className="astro-auto-rule-row">
            <div className="astro-auto-rule-index">04</div>
            <div className="astro-auto-rule-copy">
              <strong>下架交易所排除</strong>
              <span>旧公告索引按币种、交易所及现货／合约市场过滤。新闻监控确认的下架限制始终执行，并检查已有卡片的禁止开仓状态</span>
            </div>
            <div className="astro-auto-rule-switch">
              <Switch checked={excludeDelistedExchangeCards} onChange={setExcludeDelistedExchangeCards} />
              <b>{excludeDelistedExchangeCards ? "已启用" : "已关闭"}</b>
            </div>
          </div>
          <div className="astro-auto-rule-row">
            <div className="astro-auto-rule-index">05</div>
            <div className="astro-auto-rule-copy">
              <strong>Pulse 初筛与行情时效</strong>
              <span>Pulse 只发现候选；热点路线随后进入直连 API 连续监控</span>
            </div>
            <label><span>热点触发</span><InputNumber disabled precision={0} value={1} addonAfter="次直连命中" /></label>
            <label><span>Pulse 报价最长有效</span><InputNumber min={5} max={120} step={1} precision={0} value={maxQuoteAgeSeconds} onChange={(value) => setMaxQuoteAgeSeconds(Number(value ?? 20))} addonAfter="秒" /></label>
          </div>
          <div className="astro-auto-rule-row astro-auto-alert-rule-row">
            <div className="astro-auto-rule-index">06</div>
            <div className="astro-auto-rule-copy">
              <strong>新卡报警设置</strong>
              <span>写入以后自动创建的 Astro 卡片；数值留空表示该项不报警</span>
            </div>
            <label><span>实时差价大于报警</span><InputNumber min={0.01} max={100} step={0.1} precision={2} value={greaterPriceAlertPct} placeholder="留空不报警" onChange={(value) => setGreaterPriceAlertPct(value === null ? null : Number(value))} addonAfter="%" /></label>
            <div className="astro-auto-price-change-control">
              <label><span>价格涨跌幅报警</span><InputNumber min={0.01} max={100} step={0.1} precision={2} value={priceChangeAlertPct} placeholder="留空不报警" onChange={(value) => { const next = value === null ? null : Number(value); setPriceChangeAlertPct(next); if (next === null) setPriceChangeAlertOnlyRise(false); }} addonAfter="%" /></label>
              <Checkbox disabled={priceChangeAlertPct === null} checked={priceChangeAlertOnlyRise} onChange={(event) => setPriceChangeAlertOnlyRise(event.target.checked)}>仅上涨</Checkbox>
            </div>
          </div>
          <div className="astro-auto-rule-row">
            <div className="astro-auto-rule-index">07</div>
            <div className="astro-auto-rule-copy">
              <strong>新卡下单范围</strong>
              <span>写入以后自动创建的 Astro 卡片；约束每笔下单金额</span>
            </div>
            <label><span>最小单笔金额</span><InputNumber min={0.01} max={1000000} step={1} precision={2} value={minNotionalUsdt} onChange={(value) => setMinNotionalUsdt(Number(value ?? 6))} addonAfter="USDT" /></label>
            <label><span>最大单笔金额</span><InputNumber min={0.01} max={1000000} step={1} precision={2} value={maxNotionalUsdt} onChange={(value) => setMaxNotionalUsdt(Number(value ?? 40))} addonAfter="USDT" /></label>
          </div>
          <div className="astro-auto-rule-row is-locked">
            <div className="astro-auto-rule-index">08</div>
            <div className="astro-auto-rule-copy">
              <strong>热点连续直连监控</strong>
              <span>首次及接近门槛优先；连续远离门槛降为 2～5 秒复查，改善时恢复快速检查。两轮真实盘口通过后建立暂停卡，实际等待见“运行状态”</span>
            </div>
            <Tag color="green">系统安全项 · 固定启用</Tag>
          </div>
        </div>
        <Typography.Paragraph type="secondary">
          新币成交额豁免只适用于明确正式上线后的两小时，并分别核对每条腿的交易所及现货／合约市场。提前公告和未知上线时间不获得豁免。
          发现差价用于筛选候选；CEX SF/FF 按单笔金额完成两轮真实盘口复核，新卡默认暂停，开仓值按最终验证价格生成。
        </Typography.Paragraph>
        {scanner?.delistingRule?.lastError ? <Alert type="error" showIcon message={scanner.delistingRule.lastError} /> : null}
      </section>

      <section className="astro-rules-card astro-rules-secondary-card">
        <Collapse
          ghost
          items={[{
            key: "basic-filters",
            label: <div className="astro-secondary-collapse-label"><span><SettingOutlined /> 基础过滤</span><small>次级设置 · 默认收起，不在主界面展示过滤日志</small></div>,
            children: (
              <div className="astro-rules-basic-grid">
                <label>
                  <span>24小时成交额门槛</span>
                  <InputNumber min={0} step={10000} value={minVolumeUsdt} onChange={(value) => setMinVolumeUsdt(Number(value ?? 0))} addonAfter="USDT" />
                </label>
                <label>
                  <span>删除后有效回弱</span>
                  <InputNumber min={0} max={100} step={0.1} value={deletePullbackPctPoints} onChange={(value) => setDeletePullbackPctPoints(Number(value ?? 0))} addonAfter="百分点" />
                  <Typography.Text type="secondary">先回弱，再重新突破正常阈值时允许重建</Typography.Text>
                </label>
                <label>
                  <span>未回弱直接突破</span>
                  <InputNumber min={0} max={1000} step={5} value={deleteRearmPct} onChange={(value) => setDeleteRearmPct(Number(value ?? 0))} addonAfter="%" />
                  <Typography.Text type="secondary">未回弱时，超过删除参考值该比例才允许重建</Typography.Text>
                </label>
                <div>
                  <span className="astro-rules-field-label">全局屏蔽币种</span>
                  <div className="astro-rules-inline-editor">
                    <Input value={blockedCoinInput} placeholder="例如 VANRY" onChange={(event) => setBlockedCoinInput(event.target.value)} onPressEnter={addBlockedCoin} />
                    <Button onClick={addBlockedCoin}>添加</Button>
                  </div>
                  <div className="astro-rules-tags">
                    {blockedCoins.length ? blockedCoins.map((coin) => (
                      <Tag key={coin} closable onClose={(event) => { event.preventDefault(); setBlockedCoins((current) => current.filter((item) => item !== coin)); }}>{coin}</Tag>
                    )) : <Typography.Text type="secondary">没有全局屏蔽币种</Typography.Text>}
                  </div>
                </div>
              </div>
            )
          }]}
        />
      </section>

      <section className="astro-rules-card astro-collapsible-market-card">
        <Collapse
          ghost
          items={[{
            key: "markets",
            label: <div className="astro-market-collapse-label"><strong>关注交易所行情</strong><Tag color="blue">已选 {markets.length}</Tag></div>,
            children: (
              <div className="astro-subscription-grid astro-rules-market-grid">
                {marketsByExchange.map(([exchangeName, exchangeMarkets]) => {
                  const selected = exchangeMarkets.filter((market) => markets.includes(market.key)).length;
                  return (
                    <div className={`astro-subscription-card ${selected ? "is-active" : ""}`} key={exchangeName}>
                      <div className="astro-subscription-card-head"><strong>{exchangeName}</strong><span>{selected}/{exchangeMarkets.length}</span></div>
                      <div className="astro-subscription-options">
                        {exchangeMarkets.map((market) => (
                          <Checkbox key={market.key} checked={markets.includes(market.key)} onChange={(event) => toggleMarket(market.key, event.target.checked)}>
                            <span className={market.marketType === "spot" ? "astro-market-spot" : "astro-market-future"}>{market.marketType === "spot" ? "现货" : "合约"}</span>
                          </Checkbox>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            )
          }]}
        />
      </section>

      <details className="astro-rules-card astro-settings-detail"><summary>定向屏蔽 · {blockedPairs.length} 条</summary>
        <div className="astro-rules-section-head">
          <strong>定向屏蔽</strong>
          <Typography.Text type="secondary">只屏蔽某个交易所市场中的指定币种，其他交易所仍参与扫描</Typography.Text>
        </div>
        <div className="astro-rules-pair-editor">
          <Select mode="multiple" allowClear showSearch optionFilterProp="label" maxTagCount="responsive" value={blockExchanges} placeholder="先选择交易所（支持多选）" options={blockExchangeOptions} onChange={setBlockExchanges} />
          <Space.Compact className="astro-block-market-type-select">
            <Select mode="multiple" allowClear maxTagCount={2} value={blockMarketTypes} placeholder="再选择现货 / 合约" options={[{ value: "spot", label: "现货" }, { value: "future", label: "合约" }]} onChange={(values) => setBlockMarketTypes(values as BlockMarketType[])} />
            <Button onClick={() => setBlockMarketTypes(["spot", "future"])}>全部</Button>
          </Space.Compact>
          <Input value={blockSymbol} placeholder="币种，例如 VANRY" onChange={(event) => setBlockSymbol(event.target.value)} onPressEnter={addBlockedPair} />
          <Button onClick={addBlockedPair}>添加屏蔽</Button>
        </div>
        <div className="astro-blocked-search-row">
          <Input allowClear prefix={<SearchOutlined />} value={blockedSearch} placeholder="搜索已屏蔽币种或交易所" onChange={(event) => setBlockedSearch(event.target.value)} />
          <Typography.Text type="secondary">显示 {visibleBlockedPairs.length} / {blockedPairs.length} 条</Typography.Text>
        </div>
        <div className="astro-blocked-rule-grid">
          {visibleBlockedPairs.length ? visibleBlockedPairs.map((item) => (
            <div className="astro-blocked-rule-row" key={`${item.marketKey}-${item.symbol}`}>
              <div><strong>{item.symbol.replace(/USDT$/, "")}</strong><span>{marketLabel(item.marketKey)}</span></div>
              <Button type="text" danger size="small" icon={<DeleteOutlined />} aria-label={`删除 ${marketLabel(item.marketKey)} ${item.symbol}`} onClick={() => setBlockedPairs((current) => current.filter((rule) => rule.marketKey !== item.marketKey || rule.symbol !== item.symbol))} />
            </div>
          )) : <Typography.Text type="secondary">没有匹配的定向屏蔽规则</Typography.Text>}
        </div>
      </details>

      <details className="astro-rules-card astro-settings-detail" id="symbol-mappings"><summary>币名映射 · 展开管理</summary>
        <div className="astro-rules-section-head">
          <div><strong>币名映射</strong> <Typography.Text type="secondary">不同交易所名称归一后才比较差价</Typography.Text></div>
          <Button onClick={() => scanMappings.mutate()} loading={scanMappings.isPending}>自动扫描映射</Button>
        </div>
        {mappingScanResult ? (
          <Alert type={mappingScanResult.errorCount ? "warning" : "success"} showIcon message={`扫描完成：新增 ${mappingScanResult.createdCount}，更新 ${mappingScanResult.updatedCount}，已存在 ${mappingScanResult.unchangedCount}`} />
        ) : null}
        <div className="astro-mapping-form">
          <label><span>统一币名</span><Input placeholder="例如 NESA" value={mappingDraft.inputSymbol} onChange={(event) => setMappingDraft((current) => ({ ...current, inputSymbol: event.target.value.toUpperCase() }))} /></label>
          <label><span>交易所</span><Select placeholder="选择交易所" value={mappingDraft.exchange ?? undefined} options={(settingsQuery.data?.availableExchanges ?? []).map((item) => ({ value: item.code, label: item.name }))} onChange={(value: CryptoExchange) => setMappingDraft((current) => ({ ...current, exchange: value }))} /></label>
          <label><span>市场</span><Select value={mappingDraft.marketType} options={marketTypeOptions} onChange={(value: CryptoMarketType) => setMappingDraft((current) => ({ ...current, marketType: value }))} /></label>
          <label><span>交易所实际币名</span><Input placeholder="例如 1000NES" value={mappingDraft.mappedSymbol} onChange={(event) => setMappingDraft((current) => ({ ...current, mappedSymbol: event.target.value.toUpperCase() }))} /></label>
          <label><span>价格倍率</span><Select value={mappingDraft.priceRatio} options={priceRatioOptions} onChange={(value: number) => setMappingDraft((current) => ({ ...current, priceRatio: value }))} /></label>
          <label><span>备注</span><Input placeholder="可选" value={mappingDraft.note} onChange={(event) => setMappingDraft((current) => ({ ...current, note: event.target.value }))} /></label>
          <div className="astro-mapping-actions">
            {editingMappingId ? <Button onClick={resetMapping}>取消编辑</Button> : null}
            <Button type="primary" onClick={saveMapping}>{editingMappingId ? "保存修改" : "添加映射"}</Button>
          </div>
        </div>
        <Typography.Paragraph type="secondary" className="astro-mapping-help">
          示例：统一币名填 NESA；若 Bybit 实际叫 1000NES，则选择 Bybit 合约、实际币名填 1000NES、价格倍率填 1000。扫描器会先除以倍率，再与其他交易所的 NESA 比较。
        </Typography.Paragraph>
        <Table rowKey="id" size="small" loading={settingsQuery.isLoading} columns={mappingColumns} dataSource={settingsQuery.data?.symbolMappings ?? []} pagination={{ pageSize: 8, size: "small" }} scroll={{ x: 760 }} />
      </details>
    </div>
  );
}
