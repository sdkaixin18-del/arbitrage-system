import { SearchOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Collapse, Input, Segmented, Select, Space, Spin, Switch, Typography, message } from "antd";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { request } from "../apiClient";
import { historyReferences } from "../lib/historyReferences";
import AstroChainLabel, { ASTRO_CHAINS } from "../components/AstroChainLabel";
import AppChart, { type AppChartRef } from "../components/AppChart";
import BookSpreadHistory from "../components/BookSpreadHistory";

interface DexHistoryPair {
  symbol: string;
  displayName: string;
  poolAddress: string;
  inputAddress: string;
  poolName: string;
  poolDex: string;
  futuresDivisor: number;
  poolSelectionMode: "pool_direct" | "token_auto";
  queryAddressType: "token" | "pool";
  historyPriceMode: "primary_pool_proxy" | "direct_pool";
  dexTokenSide: "base" | "quote";
  dexNetwork: DexNetwork;
  dexNetworkLabel: string;
  futuresVenue: FuturesVenue;
  futuresVenueLabel: string;
  futuresBaseSymbol: string;
  futuresSymbol: string;
}

interface DexHistoryPoint {
  timestamp: number;
  dexClose: number;
  futuresClose: number;
  spreadPct: number;
}

interface DexHistoryPayload {
  observedAt: string;
  pair: DexHistoryPair;
  rangeHours: number;
  granularity: string;
  sources: { dex: string; futures: string };
  spreadDefinition: {
    rule: "astro_symmetric";
    buyLeg: "dex";
    sellLeg: "futures";
    priceBasis: "aligned_kline_close";
    formula: string;
    positiveMeaning: string;
    negativeMeaning: string;
    executionDifference: string;
  };
  dataPolicy: string;
  points: DexHistoryPoint[];
  stats: {
    latest: number;
    high: number;
    low: number;
    average: number;
    pointCount: number;
    coveragePct: number;
    expectedPointCount: number;
  };
}

const RANGE_OPTIONS = [
  { label: "4小时", value: 4 },
  { label: "1天", value: 24 },
  { label: "3天", value: 72 },
  { label: "7天", value: 168 },
  { label: "30天", value: 720 }
];

type DexNetwork = typeof ASTRO_CHAINS[number]["network"];
type FuturesVenue = "bg" | "bn" | "gt" | "okx" | "aster" | "by";

const DEX_NETWORK_OPTIONS = ASTRO_CHAINS.map(chain => ({ value: chain.network, label: chain.label }));

const FUTURES_VENUE_OPTIONS: Array<{ value: FuturesVenue; label: string }> = [
  { value: "bg", label: "BG · Bitget" },
  { value: "bn", label: "BN · Binance" },
  { value: "gt", label: "GT · Gate" },
  { value: "okx", label: "OKX" },
  { value: "aster", label: "Aster" },
  { value: "by", label: "BY · Bybit" }
];

const beijingTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false
});

function percent(value: number | null | undefined, digits = 3) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function price(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 100) return value.toFixed(2);
  if (Math.abs(value) >= 1) return value.toFixed(4);
  return value.toPrecision(6);
}

function readableError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  try {
    const parsed = JSON.parse(message) as { detail?: string };
    return parsed.detail || message;
  } catch {
    return message;
  }
}

type HistoryQuery = {poolAddress: string; dexSymbol: string; dexNetwork: DexNetwork; futuresVenue: FuturesVenue;
  futuresSymbol: string; rangeHours: number; futuresDivisor: number; referenceReadAt?: string; cardLabel?: string; openTargetPct?: number | null; closeTargetPct?: number | null};
type Asset = {id: string; symbol: string; symbols: string[]; dexNetwork: DexNetwork; chainIndex: string; chainLabel: string; contractAddress: string};
type RouteCard = {id: string; symbol: string; buyExchange: string; sellExchange: string; futuresVenue: FuturesVenue;
  futuresSymbol: string; futuresDivisor: number; assetIds: string[]; resolution: string; openTargetPct?: number | null; closeTargetPct?: number | null};
type Catalog = {observedAt?: string; assets: Asset[]; cards: RouteCard[]; warnings: string[]};
const validLevel = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
function referenceDistance(spread: number, level: number) {
  const delta = spread - level;
  return Math.abs(delta) < 0.0005 ? "与参考线基本重合" : `${delta > 0 ? "高于" : "低于"}该线 ${Math.abs(delta).toFixed(3)} 个百分点`;
}
const RECENTS_KEY = "astro-dex-history-recents-v1";
const venueLabel = (v: string) => v === "pancakeswapv3" ? "PancakeSwap V3" : v === "okxdex" ? "OKXDEX" : v;
const cardLabel = (card: RouteCard) => `${card.symbol} · ${venueLabel(card.buyExchange)} / ${card.sellExchange}`;
function initialQuery(): HistoryQuery {
  const p = new URLSearchParams(location.search);
  return {poolAddress: p.get("poolAddress") || "", dexSymbol: p.get("dexSymbol") || "",
    dexNetwork: DEX_NETWORK_OPTIONS.find(o=>o.value === p.get("dexNetwork"))?.value || "solana",
    futuresVenue: FUTURES_VENUE_OPTIONS.find(o=>o.value === p.get("futuresVenue"))?.value || "bn",
    futuresSymbol: p.get("futuresSymbol") || "", rangeHours: RANGE_OPTIONS.find(o=>o.value === Number(p.get("rangeHours")))?.value || 24,
    futuresDivisor: Number(p.get("futuresDivisor") || 1)};
}
function readRecents(): HistoryQuery[] {
  try {const value = JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]");
    return Array.isArray(value) ? value.filter(q=>q && typeof q.poolAddress === "string" && typeof q.dexSymbol === "string"
      && DEX_NETWORK_OPTIONS.some(o=>o.value === q.dexNetwork) && FUTURES_VENUE_OPTIONS.some(o=>o.value === q.futuresVenue)).slice(0,8) : [];
  } catch {return [];}
}
function queryParams(q: HistoryQuery) {
  return new URLSearchParams({poolAddress:q.poolAddress.trim(),dexSymbol:q.dexSymbol.trim(),dexNetwork:q.dexNetwork,
    futuresVenue:q.futuresVenue,futuresSymbol:q.futuresSymbol.trim(),rangeHours:String(q.rangeHours),futuresDivisor:String(q.futuresDivisor || 1)});
}
function DexPriceHistoryPage() {
  const [query, setQuery] = useState<HistoryQuery>(initialQuery);
  const {poolAddress,dexSymbol,dexNetwork,futuresVenue,futuresSymbol,rangeHours} = query;
  const [payload, setPayload] = useState<DexHistoryPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [showReferences,setShowReferences] = useState(true);
  const [referenceMode,setReferenceMode] = useState<"history" | "card">("history");
  const [moreQueryOpen,setMoreQueryOpen] = useState(()=>!!initialQuery().poolAddress && !initialQuery().futuresSymbol);
  const [error, setError] = useState<string | null>(null);
  const [catalog,setCatalog] = useState<Catalog | null>(null);
  const [catalogLoading,setCatalogLoading] = useState(false);
  const [catalogError,setCatalogError] = useState("");
  const [selectedCard,setSelectedCard] = useState<RouteCard | null>(null);
  const [quickCopy,setQuickCopy] = useState("");
  const [recents,setRecents] = useState<HistoryQuery[]>(readRecents);
  const chartRef = useRef<AppChartRef>(null);
  const chartHost = useRef<HTMLDivElement>(null);
  const sequence = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const canQuery = !!(poolAddress.trim() && dexSymbol.trim() && futuresSymbol.trim());
  const invalidate = () => {sequence.current++;controller.current?.abort();setLoading(false);setPayload(null);setError(null);};
  const changeQuery = (patch: Partial<HistoryQuery>) => {
    invalidate();setQuery(q=>({...q,...patch,cardLabel:undefined,referenceReadAt:undefined,openTargetPct:null,closeTargetPct:null}));setSelectedCard(null);
  };
  const loadCatalog = async () => {
    setCatalogLoading(true);setCatalogError("");
    try {setCatalog(await request<Catalog>("/api/dex-history/routes"));}
    catch {setCatalogError("卡片读取失败，仍可使用最近查询或手动填写");}
    finally {setCatalogLoading(false);}
  };
  const performQuery = async (q: HistoryQuery) => {
    if (!(q.poolAddress.trim() && q.dexSymbol.trim() && q.futuresSymbol.trim())) return;
    controller.current?.abort();const active = ++sequence.current;const abort = new AbortController();controller.current = abort;
    setQuery(q);setLoading(true);setError(null);setPayload(null);
    const params = queryParams(q);
    history.replaceState(null,"",`${location.pathname}?${params}`);
    try {
      // Native fetch keeps deliberately cancelled requests out of the runtime error log.
      const response = await fetch(`/api/dex-history?${params}`,{signal:abort.signal});
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json() as DexHistoryPayload;
      if (active !== sequence.current) return;
      setPayload(result);
      const next = [q,...readRecents().filter(old=>queryParams(old).toString() !== params.toString())].slice(0,8);
      setRecents(next);try {localStorage.setItem(RECENTS_KEY,JSON.stringify(next));} catch { /* optional browser storage */ }
    } catch (reason) {
      if (active === sequence.current && !abort.signal.aborted) setError(readableError(reason));
    } finally {if (active === sequence.current) setLoading(false);}
  };
  const runQuery = (event?: FormEvent) => {event?.preventDefault();if(canQuery){setMoreQueryOpen(false);void performQuery(query);}};
  useEffect(()=>{void loadCatalog();if (new URLSearchParams(location.search).get("futuresSymbol")) void performQuery(initialQuery());
    return ()=>{sequence.current++;controller.current?.abort();};},[]);
  const applyCardAsset = (card: RouteCard,asset: Asset) => {
    const q: HistoryQuery = {...query,poolAddress:asset.contractAddress,dexSymbol:card.symbol,dexNetwork:asset.dexNetwork,
      futuresVenue:card.futuresVenue,futuresSymbol:card.futuresSymbol,futuresDivisor:card.futuresDivisor,
      cardLabel:cardLabel(card),referenceReadAt:catalog?.observedAt,openTargetPct:card.openTargetPct,closeTargetPct:card.closeTargetPct};
    setSelectedCard(card);setMoreQueryOpen(false);void performQuery(q);
  };
  const chooseCard = (card: RouteCard) => {
    invalidate();setSelectedCard(card);
    const assets = catalog?.assets.filter(a=>card.assetIds.includes(a.id)) || [];
    if (assets.length === 1) applyCardAsset(card,assets[0]);
    else {setMoreQueryOpen(true);setQuery(q=>({...q,poolAddress:"",dexSymbol:card.symbol,futuresVenue:card.futuresVenue,futuresSymbol:card.futuresSymbol,
      futuresDivisor:card.futuresDivisor,cardLabel:cardLabel(card),referenceReadAt:catalog?.observedAt,openTargetPct:card.openTargetPct,closeTargetPct:card.closeTargetPct}));}
  };
  const parseQuickCopy = () => {
    try {
      const decoded = quickCopy.replace(/&#x20;|&#32;|&nbsp;/gi," ").replace(/&quot;/g,'"').replace(/^\s*ASTRO-QUICK-COPY:\s*/i,"");
      const value = JSON.parse(decoded);
      if (value.type !== "SF" || !["okxdex","pancakeswapv3"].includes(value.buyEx)) throw new Error("请粘贴 OKXDEX 或 PancakeSwap V3 的 SF 卡片");
      const symbol = String(value.name || "").toUpperCase();
      const found = catalog?.cards.find(c=>c.symbol === symbol && c.buyExchange === value.buyEx && c.sellExchange === value.sellEx);
      if (!found) throw new Error("没有找到对应的当前卡片，请刷新卡片或从已配置币种选择");
      chooseCard(found);setQuickCopy("");
    } catch (e) {message.error(e instanceof Error ? e.message : "复制内容格式不正确");}
  };

  useEffect(()=>{
    const node=chartHost.current;if(!node)return;
    const observer=new ResizeObserver(()=>chartRef.current?.getEchartsInstance().resize());
    observer.observe(node);return ()=>observer.disconnect();
  },[payload,loading]);
  const intervalMs = payload?.granularity === "1m" ? 60000 : payload?.granularity === "5m" ? 300000 : 900000;
  const historical = useMemo(()=>historyReferences(payload?.points ?? [],intervalMs),[payload,intervalMs]);
  const hasCardReferences = validLevel(query.openTargetPct) || validLevel(query.closeTargetPct);
  const activeReferenceMode = referenceMode === "card" && hasCardReferences ? "card" : "history";
  const openLevel = activeReferenceMode === "history" ? historical.open : query.openTargetPct;
  const closeLevel = activeReferenceMode === "history" ? historical.close : query.closeTargetPct;
  const referenceLabel = activeReferenceMode === "history" ? "历史" : "卡片";
  const spreadOption = useMemo(() => {
    const points = payload?.points ?? [];
    const intervalMs = payload?.granularity === "1m" ? 60000 : payload?.granularity === "5m" ? 300000 : 900000;
    const chartPoints = points.flatMap((item,index)=>{
      const point = {value:[item.timestamp,item.spreadPct],dexClose:item.dexClose,futuresClose:item.futuresClose};
      return index > 0 && item.timestamp - points[index-1].timestamp > intervalMs
        ? [{value:[points[index-1].timestamp + intervalMs,null],dexClose:undefined,futuresClose:undefined},point] : [point];
    });
    const visibleLevels = showReferences ? [openLevel,closeLevel].filter(validLevel) : [];
    const axisExtent = (extent: {min:number;max:number}, side: "min" | "max") => {
      const low=Math.min(extent.min,...visibleLevels),high=Math.max(extent.max,...visibleLevels);
      const padding=Math.max(0.05,(high-low)*0.1);
      return side === "min" ? low-padding : high+padding;
    };
    return {
      animation: false,
      grid: { left: 64, right: 24, top: 30, bottom: 62 },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (items: unknown) => {
          const rows = items as Array<{ data?: { value?: [number, number]; dexClose?: number; futuresClose?: number } }>;
          const item = rows[0]?.data;
          if (!item?.value || !validLevel(item.value[1])) return "";
          return [
            `<strong>${beijingTime.format(new Date(item.value[0]))}</strong>`,
            `Astro 对称差价：${percent(item.value[1])}`,
            `DEX：${price(item.dexClose)}`,
            `${payload?.pair.futuresVenueLabel ?? "交易所"}：${price(item.futuresClose)}`,
            ...(showReferences && validLevel(openLevel) ? [`${referenceLabel}开仓参考 ${percent(openLevel)}：${referenceDistance(item.value[1],openLevel)}`] : []),
            ...(showReferences && validLevel(closeLevel) ? [`${referenceLabel}平仓参考 ${percent(closeLevel)}：${referenceDistance(item.value[1],closeLevel)}`] : [])
          ].join("<br/>");
        }
      },
      xAxis: {
        type: "time",
        axisLine: { lineStyle: { color: "#d4d9d6" } },
        axisLabel: { color: "#7b8580", formatter: (value: number) => beijingTime.format(new Date(value)) },
        splitLine: { show: false }
      },
      yAxis: {
        type: "value",
        name: "%",
        scale: true,
        min: (extent: {min:number;max:number}) => axisExtent(extent,"min"),
        max: (extent: {min:number;max:number}) => axisExtent(extent,"max"),
        axisLabel: { color: "#7b8580", formatter: (value: number) => value.toFixed(2) },
        splitLine: { lineStyle: { color: "#eff1f0" } }
      },
      dataZoom: [
        { type: "inside", filterMode: "none" },
        { type: "slider", height: 20, bottom: 14, borderColor: "#e1e5e2", fillerColor: "rgba(22, 119, 255, 0.10)" }
      ],
      series: [
        {
          type: "line",
          showSymbol: true,
          symbolSize: 3,
          sampling: "lttb",
          lineStyle: { width: 1.6, color: "#1677ff" },
          areaStyle: { color: "rgba(22, 119, 255, 0.05)" },
          markLine: {
            silent: true,
            symbol: "none",
            label: { show: true, fontSize: 12, fontWeight: 700, padding: [5,8], borderRadius: 4, formatter: "{b}" },
            data: [{ yAxis: 0, name:"零差价", label:{show:false},lineStyle:{color:"#c3cbc7",width:1,type:"dotted"} },
              ...(showReferences && validLevel(openLevel) ? [{yAxis:openLevel,
                name:`${referenceLabel}开仓参考 ≥ ${percent(openLevel)}`,
                lineStyle:{color:"#c76a0b",width:2.5,type:"dashed"},
                label:{position:"insideStartTop",color:"#994600",backgroundColor:"#fff4e6",borderColor:"#e0a25b",borderWidth:1}}] : []),
              ...(showReferences && validLevel(closeLevel) ? [{yAxis:closeLevel,
                name:`${referenceLabel}平仓参考 ≤ ${percent(closeLevel)}`,
                lineStyle:{color:"#00856a",width:2.5,type:"solid"},
                label:{position:"insideEndBottom",color:"#00644f",backgroundColor:"#e8f8f2",borderColor:"#58ae95",borderWidth:1}}] : [])]

          },
          connectNulls: false,
          data: chartPoints
        }
      ]
    };
  }, [payload,openLevel,closeLevel,referenceLabel,showReferences]);

  const latestPoint = payload?.points[payload.points.length - 1];
  const hasReferences = validLevel(openLevel) || validLevel(closeLevel);
  const referenceTime = query.referenceReadAt && Number.isFinite(Date.parse(query.referenceReadAt))
    ? beijingTime.format(new Date(query.referenceReadAt)) : null;

  return (
    <section className="dex-history-page is-compact is-simplified">
      <Card className="dex-history-shortcuts" size="small">
        <Space wrap><Typography.Text strong>我的 SF 卡片</Typography.Text>
          <Button size="small" loading={catalogLoading} onClick={()=>void loadCatalog()}>刷新卡片</Button>
          <Button size="small" href="/astro/dex">币种配置</Button>
</Space>
        {catalogError && <Alert type="warning" message={catalogError} />}
        {catalog?.warnings.map(w=><Alert key={w} type="warning" message={w} />)}
        <div className="dex-history-card-list">{catalog?.cards.map(card=><Button key={card.id}
          type={selectedCard?.id === card.id ? "primary" : "default"} onClick={()=>chooseCard(card)}>
          {cardLabel(card)}{card.resolution !== "resolved" ? " · 选择地址" : ""}</Button>)}</div>
        {!catalogLoading && catalog && !catalog.cards.length && <Typography.Text type="secondary">暂无 SF 卡片，展开“其他查询方式”选择币种。</Typography.Text>}
        {selectedCard && selectedCard.assetIds.length !== 1 && <Alert type="info" showIcon
          message={selectedCard.assetIds.length ? "同名币存在多条链或多个地址，请选择本卡片使用的币种" : "此卡片缺少链地址，请手动填写，或先在币种配置中登记"} />}
        <Collapse ghost size="small" className="dex-history-more-query" activeKey={moreQueryOpen ? ["query"] : []}
          onChange={keys=>setMoreQueryOpen(keys.includes("query"))}
          items={[{key:"query",label:"其他查询方式 · 选币 / 手动输入 / 最近查询",children:<div>
        <Select showSearch optionFilterProp="label" allowClear placeholder="搜索已配置币种 / 链 / 合约地址" style={{width:"100%"}}
          value={undefined} options={(catalog?.assets || []).filter(a=>!selectedCard || selectedCard.assetIds.length <= 1 || selectedCard.assetIds.includes(a.id))
            .map(a=>({value:a.id,label:`${a.symbols.join(" / ")} · ${a.chainLabel} · ${a.contractAddress}`}))}
          onChange={id=>{const asset=catalog?.assets.find(a=>a.id === id);if(!asset)return;
            if(selectedCard && selectedCard.assetIds.includes(asset.id)) applyCardAsset(selectedCard,asset);
            else changeQuery({poolAddress:asset.contractAddress,dexSymbol:asset.symbol,dexNetwork:asset.dexNetwork,futuresSymbol:asset.symbol,futuresDivisor:1});}} />
        {recents.length > 0 && <div className="dex-history-recents"><Typography.Text type="secondary">最近查询</Typography.Text>
          {recents.map((q,index)=><Button size="small" key={index} onClick={()=>{setSelectedCard(null);setMoreQueryOpen(false);void performQuery(q);}}>
            {q.dexSymbol} · {DEX_NETWORK_OPTIONS.find(o=>o.value===q.dexNetwork)?.label} / {q.futuresVenue}</Button>)}</div>}
        <Collapse ghost size="small" items={[{key:"copy",label:"粘贴 Astro 卡片复制内容",children:<Space.Compact style={{width:"100%"}}>
          <Input.TextArea value={quickCopy} onChange={e=>setQuickCopy(e.target.value)} rows={3} placeholder="ASTRO-QUICK-COPY: {...}" />
          <Button disabled={!catalog || !quickCopy.trim()} onClick={parseQuickCopy}>识别查询</Button></Space.Compact>}]} />
      <form onSubmit={runQuery}>
        <div className="dex-history-query-grid">
          <Card className="dex-history-side-card" bordered>
            <div className="dex-history-side-title"><strong>左侧</strong><span>DEX 现货</span></div>
            <label>链</label>
            <Select
              value={dexNetwork}
              onChange={(value) => {
                changeQuery({dexNetwork:value,poolAddress:""});
              }}
              options={DEX_NETWORK_OPTIONS}
              virtual={false}
              listHeight={280}
              optionRender={option => <AstroChainLabel chain={String(option.value)} />}
              labelRender={option => <AstroChainLabel chain={String(option.value)} />}
            />
            <label>币名</label>
            <Input value={dexSymbol} onChange={(event) => changeQuery({dexSymbol:event.target.value.toUpperCase()})} placeholder="例如 哈基米、STONKS" />
            <label>交易代币地址 / Mint</label>
            <Input
              value={poolAddress}
              onChange={(event) => changeQuery({poolAddress:event.target.value})}
              placeholder={dexNetwork === "solana" ? "粘贴你交易使用的代币 Mint" : "粘贴你交易使用的 0x 代币合约"}
              allowClear
            />
            <Typography.Text type="secondary" className="dex-history-field-note">
              直接填写你的交易代币地址；系统自动选择历史K线主池。池地址或 GeckoTerminal 池链接也兼容。
            </Typography.Text>
          </Card>

          <Card className="dex-history-side-card" bordered>
            <div className="dex-history-side-title"><strong>右侧</strong><span>永续合约</span></div>
            <label>交易所</label>
            <Select value={futuresVenue} onChange={value=>changeQuery({futuresVenue:value,futuresDivisor:1})} options={FUTURES_VENUE_OPTIONS} />
            <label>币名或交易对</label>
            <Input
              value={futuresSymbol}
              onChange={(event) => changeQuery({futuresSymbol:event.target.value.toUpperCase(),futuresDivisor:1})}
              placeholder="例如 INTC、INTCUSDT 或 INTC/USDT"
              allowClear
            />
            <Typography.Text type="secondary" className="dex-history-field-note">
              合约代码按实际交易所填写；从卡片选择时自动带入。{query.futuresDivisor !== 1 ? `价格按每币换算：合约价格 ÷ ${query.futuresDivisor}` : ""}
            </Typography.Text>
          </Card>
        </div>

        <div className="dex-history-query-actions">
          <Button type="primary" htmlType="submit" icon={<SearchOutlined />} loading={loading} disabled={!canQuery}>查询</Button>
          <Button disabled={!canQuery} onClick={()=>{navigator.clipboard.writeText(`${location.origin}${location.pathname}?${queryParams(query)}`).then(()=>message.success("查询链接已复制"),()=>message.error("复制失败，可复制地址栏链接"));}}>复制查询链接</Button>
        </div>
      </form>
          </div>}]} />
      </Card>
      <div className="dex-history-range-toolbar">
          <Segmented options={RANGE_OPTIONS} value={rangeHours} onChange={(value) => {
            const next={...query,rangeHours:Number(value)};if(canQuery) void performQuery(next);else setQuery(next);
          }} />

        <Button size="small" disabled={!canQuery} loading={loading} onClick={()=>void performQuery(query)}>刷新行情</Button>
      </div>

      {error ? <Alert type="error" showIcon message="查询失败" description={error} /> : null}

      {loading ? <div className="dex-history-empty"><Spin size="small" /><span>正在读取两端云端 K 线…</span></div> : null}

      {!loading && !payload && !error ? <div className="dex-history-empty">点选 SF 卡片，或展开“其他查询方式”</div> : null}

      {payload && !loading ? (
        <Card className="dex-history-result-card" bordered>
          <div className="dex-history-result-head">
            <div><strong>{query.cardLabel || `${payload.pair.symbol} · ${payload.pair.futuresVenueLabel}`}</strong>
              <span><AstroChainLabel chain={payload.pair.dexNetwork} /> · 参考池：{({pancakeswap_v2:"PancakeSwap V2","pancakeswap-v3-bsc":"PancakeSwap V3"} as Record<string,string>)[payload.pair.poolDex] || payload.pair.poolDex}</span></div>
            <div className="dex-history-result-meta">{payload.granularity} · 覆盖 {payload.stats.coveragePct.toFixed(1)}%</div>
          </div>
          {payload.stats.coveragePct < 50 ? (
            <Alert
              className="dex-history-sparse-alert"
              type="info"
              showIcon
              message="DEX 成交稀疏：无成交分钟没有 K 线，系统未使用本地数据或插值补点。"
            />
          ) : null}

          <div className="dex-reference-panel">
            <div className="dex-reference-heading">
              <Segmented size="small" aria-label="参考线来源" value={activeReferenceMode}
                onChange={value=>setReferenceMode(value as "history" | "card")}
                options={[{label:"历史计算",value:"history"},{label:"卡片设置",value:"card",disabled:!hasCardReferences}]} />
              <Space size={6}><span>显示参考线</span><Switch size="small" aria-label="显示参考线" checked={showReferences}
                disabled={!hasReferences} onChange={setShowReferences} /></Space>
            </div>
            {hasReferences ? <>
              <div className="dex-reference-grid">
                <div className="dex-reference-value is-open"><span><i />{referenceLabel}开仓参考 · 价差 ≥</span>
                  <strong>{percent(openLevel)}</strong>
                  <small>{validLevel(openLevel) && latestPoint ? referenceDistance(latestPoint.spreadPct,openLevel) : "卡片未设置开仓参考"}</small></div>
                <div className="dex-reference-value is-close"><span><i />{referenceLabel}平仓参考 · 价差 ≤</span>
                  <strong>{percent(closeLevel)}</strong>
                  <small>{validLevel(closeLevel) && latestPoint ? referenceDistance(latestPoint.spreadPct,closeLevel) : "卡片未设置平仓参考"}</small></div>
                <div className="dex-reference-value is-latest"><span><i />最近历史差价</span>
                  <strong>{percent(latestPoint?.spreadPct)}</strong><small>{latestPoint ? `${beijingTime.format(new Date(latestPoint.timestamp))} · 已结束 K 线` : "—"}</small></div>
              </div>

            </> : <div className="dex-reference-manual"><strong>{percent(latestPoint?.spreadPct)}</strong><span>{activeReferenceMode === "history" ? historical.reason : "卡片未设置参考值"}</span></div>}
            {activeReferenceMode === "history" && hasReferences && <div className="dex-history-footnote">
              开仓 P90 · 平仓 P50 · {historical.samples} 个历史点 · 完成回落 {historical.completed} 次
              {historical.medianMinutes !== null ? ` · 中位用时 ${historical.medianMinutes.toFixed(1)} 分钟` : " · 暂无完整回落样本"}
            </div>}
          </div>
          <div ref={chartHost} className="dex-history-chart"><AppChart ref={chartRef} option={spreadOption} style={{ height: 410, width:"100%" }} notMerge /></div>
          <div className="dex-history-footnote">历史参考来自 K 线，非真实开仓／清仓报价；未扣手续费、资金费和滑点，触线不代表可盈利成交。</div>
          <Collapse ghost size="small" className="dex-history-details" items={[{key:"details",label:"数据详情 · 来源 / 地址 / 计算口径",children:<div>
            <div className="dex-history-result-stats">
              <span>最高 <b>{percent(payload.stats.high)}</b></span><span>最低 <b>{percent(payload.stats.low)}</b></span>
              <span>均值 <b>{percent(payload.stats.average)}</b></span>
            </div>
            <p>{payload.sources.dex} / {payload.sources.futures}</p>
            <p>{payload.stats.pointCount}/{payload.stats.expectedPointCount} 个时间点 · 覆盖率 {payload.stats.coveragePct.toFixed(1)}%
              {latestPoint ? ` · 更新至 ${beijingTime.format(new Date(latestPoint.timestamp))}` : ""}</p>
            <p>历史计算基于当前所选区间：开仓 P90 表示约 90% 的历史差价不高于该值；平仓 P50 是历史差价中位数。少于 30 个有效点不生成历史参考，查询和卡片功能仍可使用。</p>
            <p>完成回落：差价达到 P90 后，在连续数据内回到 P50 或更低。未回落 {historical.unresolved} 次，数据中断 {historical.interrupted} 次。阈值取自同一段历史，这是样本内描述，不是未来胜率或策略回测。</p>
            {payload.stats.coveragePct < 50 && <p>当前覆盖率偏低，参考值仅反映有数据的时段。</p>}
            {hasCardReferences && <p>{query.cardLabel} · 卡片开仓 {percent(query.openTargetPct)} / 平仓 {percent(query.closeTargetPct)} · {referenceTime ? `参数读取于 ${referenceTime}` : "最近查询保存的参数"}；历史计算不修改卡片设置。</p>}
          <div className="dex-history-formula">
            <b>计算：</b><span>{payload.spreadDefinition.formula}</span>
            <small>正值：{payload.spreadDefinition.positiveMeaning}；负值：{payload.spreadDefinition.negativeMeaning}。</small>
          </div>

          {payload.pair.queryAddressType === "token" ? (
            <div className="dex-history-address-explain">
              <span>查询代币地址：<b>{payload.pair.inputAddress}</b></span>
              <span>历史K线主池：<b>{payload.pair.poolName}</b>（{payload.pair.poolAddress}）</span>
              <small>同链同池共用此参考曲线；它不等于 OKXDEX 聚合路由或 PancakeSwap 实时可成交价。</small>
            </div>
          ) : null}


            <p>{payload.spreadDefinition.executionDifference}；未计滑点、Gas、交易费及资金费率。</p>
          </div>}]} />
        </Card>
      ) : null}
    </section>
  );
}

export default function DexHistoryPage() {
  const [view,setView]=useState<"books"|"prices">(()=>{
    const p=new URLSearchParams(location.search);
    return p.get("view")==="prices" || (!p.has("view")&&p.has("poolAddress")) ? "prices" : "books";
  });
  const switchView=(next:"books"|"prices",query?:URLSearchParams)=>{
    const p=query||new URLSearchParams();p.set("view",next);
    history.replaceState(null,"",`${location.pathname}?${p}`);setView(next);
  };
  return <section className="dex-history-workspace">
    <header className="dex-history-simple-head"><div><Typography.Title level={3}>DEX / 交易所历史差价查询</Typography.Title>
      <Typography.Text type="secondary">查看 SF / FF 开仓、平仓差价与 DEX 价格参考。</Typography.Text></div></header>
    <Segmented className="book-history-view-switch" aria-label="历史数据视图" value={view} onChange={v=>switchView(v as typeof view)}
      options={[{label:"开仓 / 平仓盘口 · SF / FF",value:"books"},{label:"DEX 主池价格参考",value:"prices"}]} />
    {view==="books"?<BookSpreadHistory onDexPrice={p=>switchView("prices",p)} />:<DexPriceHistoryPage />}
  </section>;
}
