import { Alert, Button, Card, Collapse, Input, InputNumber, Segmented, Select, Space, Spin, Switch, Typography, message } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import AppChart, {type AppChartRef} from "./AppChart";
import { bookGap, bookGapStats } from "../lib/bookGapStats";

type PairType = "SF" | "FF";
type Query = { pairType: PairType; leftVenue: string; rightVenue: string; leftSymbol: string; rightSymbol: string;
  leftDivisor: number; rightDivisor: number; rangeHours: number };
type RouteCard = {id: string; type: PairType; symbol: string; buyExchange: string; sellExchange: string;
  leftSymbol: string; rightSymbol: string; leftDivisor: number; rightDivisor: number};
type Catalog = {bookCards?: RouteCard[]; warnings?: string[];
  cards: Array<{id:string;symbol:string;assetIds:string[];futuresVenue:string;futuresSymbol:string;futuresDivisor:number}>;
  assets: Array<{id:string;contractAddress:string;dexNetwork:string}>};
type Point = {timestamp:number;openSpreadPct:number;closeSpreadPct:number;leftBid:number;leftAsk:number;rightBid:number;rightAsk:number};
type Payload = {status:"ok"|"unsupported"|"empty";message:string;source:string;sourceUrl:string;observedAt:string;
  requestedStart:number;requestedEnd:number;bucketSeconds:number;points:Point[];
  pair: {type:PairType;leftLabel:string;rightLabel:string;leftSymbol:string;rightSymbol:string};
  stats?:{coveragePct:number;pointCount:number;expectedPointCount:number;internalMissingBuckets:number;firstTimestamp:number;lastTimestamp:number};
  quality?:{invalidBars:number;conflictingBars:number;incremental:boolean};definition?:{open:string;close:string}};
const VENUES = [{value:"binance",label:"Binance"},{value:"gate",label:"Gate"},{value:"bitget",label:"Bitget"},
  {value:"bybit",label:"Bybit"},{value:"okx",label:"OKX"},{value:"aster",label:"Aster"},
  {value:"hyperliquid",label:"Hyperliquid"},{value:"lighter",label:"Lighter"},{value:"backpack",label:"Backpack"}];
const SPOT_VENUES = [...VENUES,{value:"okxdex",label:"OKXDEX"},{value:"pancakeswapv3",label:"PancakeSwap V3"}];
const RANGES = [{value:4,label:"4小时"},{value:24,label:"1天"},{value:72,label:"3天"},{value:168,label:"7天"},{value:720,label:"30天"}];
const label = (venue:string) => SPOT_VENUES.find(v=>v.value===venue)?.label || venue;
const formatTime = (time:number) => new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(time);
const pct = (v:number|null|undefined) => typeof v === "number" && Number.isFinite(v) ? `${v>0?"+":""}${v.toFixed(3)}%` : "—";
const pp = (v:number|null|undefined) => typeof v === "number" && Number.isFinite(v) ? `${v.toFixed(3)} 个百分点` : "—";
const price = (v:number) => v.toPrecision(7);
const escapeHtml = (s:string) => s.replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]!));
const RECENTS_KEY = "astro-book-history-recents-v1";
const params = (q:Query) => new URLSearchParams({...Object.fromEntries(Object.entries(q).map(([k,v])=>[k,String(v)])),view:"books"});
function initial():Query {
  const p = new URLSearchParams(location.search);
  const divisor = (key:string) => {const n=Number(p.get(key)||1);return Number.isFinite(n)&&n>0&&n<=1e12?n:1;};
  return {pairType:p.get("pairType")==="SF"?"SF":"FF",leftVenue:SPOT_VENUES.some(v=>v.value===p.get("leftVenue"))?p.get("leftVenue")!:"aster",
    rightVenue:VENUES.some(v=>v.value===p.get("rightVenue"))?p.get("rightVenue")!:"gate",leftSymbol:p.get("leftSymbol")||"",rightSymbol:p.get("rightSymbol")||"",
    leftDivisor:divisor("leftDivisor"),rightDivisor:divisor("rightDivisor"),rangeHours:RANGES.some(r=>r.value===Number(p.get("rangeHours")))?Number(p.get("rangeHours")):24};
}
function recents():Query[] {
  try {const value=JSON.parse(localStorage.getItem(RECENTS_KEY)||"[]");return Array.isArray(value)?value.filter(q=>q&&["SF","FF"].includes(q.pairType)&&typeof q.leftSymbol==="string"&&typeof q.rightSymbol==="string"&&SPOT_VENUES.some(v=>v.value===q.leftVenue)&&VENUES.some(v=>v.value===q.rightVenue)&&RANGES.some(r=>r.value===q.rangeHours)&&[q.leftDivisor,q.rightDivisor].every(n=>typeof n==="number"&&Number.isFinite(n)&&n>0&&n<=1e12)).slice(0,6):[];}catch{return [];}
}
const quantile = (values:number[],p:number) => {const sorted=[...values].sort((a,b)=>a-b);const i=(sorted.length-1)*p,low=Math.floor(i);return sorted[low]+(sorted[Math.min(low+1,sorted.length-1)]-sorted[low])*(i-low);};
async function jsonRead<T>(url:string,signal?:AbortSignal):Promise<T> {
  const response=await fetch(url,{signal});
  const data=await response.json();
  if(!response.ok)throw new Error(typeof data.detail==="string"?data.detail:"查询失败，请稍后重试");
  return data as T;
}

export default function BookSpreadHistory({onDexPrice}:{onDexPrice:(params?:URLSearchParams)=>void}) {
  const [query,setQuery]=useState<Query>(initial);
  const [payload,setPayload]=useState<Payload|null>(null);
  const [loading,setLoading]=useState(false);
  const [error,setError]=useState("");
  const [catalog,setCatalog]=useState<Catalog|null>(null);
  const [catalogError,setCatalogError]=useState("");
  const [catalogLoading,setCatalogLoading]=useState(false);
  const [selected,setSelected]=useState<RouteCard|null>(null);
  const [copy,setCopy]=useState("");
  const [saved,setSaved]=useState<Query[]>(recents);
  const [references,setReferences]=useState(true);
  const [lines,setLines]=useState<"both"|"open"|"close">("both");
  const seq=useRef(0),abort=useRef<AbortController|null>(null);
  const chartRef=useRef<AppChartRef>(null),chartHost=useRef<HTMLDivElement>(null);
  const canQuery=!!query.leftSymbol.trim()&&!!query.rightSymbol.trim();
  const change=(patch:Partial<Query>)=>{seq.current++;abort.current?.abort();setLoading(false);setPayload(null);setError("");setSelected(null);setQuery(q=>({...q,...patch}));};
  const loadCatalog=async()=>{setCatalogLoading(true);try{setCatalog(await jsonRead<Catalog>("/api/dex-history/routes"));setCatalogError("");}catch{setCatalogError("卡片暂时读不到，仍可手动查询");}finally{setCatalogLoading(false);}};
  const run=async(q:Query)=>{
    if(!q.leftSymbol.trim()||!q.rightSymbol.trim())return;
    abort.current?.abort();const ac=new AbortController();abort.current=ac;const active=++seq.current;
    setQuery(q);setPayload(null);setError("");setLoading(true);
    history.replaceState(null,"",`${location.pathname}?${params(q)}`);
    try{
      const value=await jsonRead<Payload>(`/api/dex-history/books?${params(q)}`,ac.signal);
      if(active!==seq.current)return;
      setPayload(value);
      if(value.status==="ok"){
        const next=[q,...recents().filter(old=>params(old).toString()!==params(q).toString())].slice(0,6);
        setSaved(next);try{localStorage.setItem(RECENTS_KEY,JSON.stringify(next));}catch{/* optional local preferences */}
      }
    }catch(e){if(active===seq.current&&!ac.signal.aborted)setError(e instanceof Error?e.message:"查询失败");}
    finally{if(active===seq.current)setLoading(false);}
  };
  useEffect(()=>{void loadCatalog();const q=initial();if(q.leftSymbol&&q.rightSymbol)void run(q);return()=>{seq.current++;abort.current?.abort();};},[]);
  useEffect(()=>{
    const host=chartHost.current;if(!host)return;
    const observer=new ResizeObserver(()=>chartRef.current?.getEchartsInstance().resize({width:host.clientWidth,height:410}));
    observer.observe(host);return()=>observer.disconnect();
  },[payload,loading]);
  const choose=(card:RouteCard)=>{setSelected(card);void run({...query,pairType:card.type,leftVenue:card.buyExchange,rightVenue:card.sellExchange,
    leftSymbol:card.leftSymbol,rightSymbol:card.rightSymbol,leftDivisor:card.leftDivisor,rightDivisor:card.rightDivisor});};
  const parseCopy=()=>{
    try{
      const value=JSON.parse(copy.replace(/&#x20;|&#32;|&nbsp;/gi," ").replace(/&quot;/g,'"').replace(/^\s*ASTRO-QUICK-COPY:\s*/i,""));
      if(!["SF","FF"].includes(value.type)||!SPOT_VENUES.some(v=>v.value===value.buyEx)||!VENUES.some(v=>v.value===value.sellEx)||typeof value.name!=="string")throw new Error("请粘贴受支持的 SF / FF 卡片");
      const symbol=value.name.trim().toUpperCase();
      const card=catalog?.bookCards?.find(c=>c.type===value.type&&c.symbol===symbol&&c.buyExchange===value.buyEx&&c.sellExchange===value.sellEx);
      if(card)choose(card);else {setSelected(null);void run({...query,pairType:value.type,leftVenue:value.buyEx,rightVenue:value.sellEx,leftSymbol:symbol,rightSymbol:symbol,leftDivisor:1,rightDivisor:1});}
      setCopy("");
    }catch(e){message.error(e instanceof Error?e.message:"复制内容格式不正确");}
  };
  const showDex=()=>{
    const route=catalog?.cards.find(c=>c.id===selected?.id);
    const asset=route?.assetIds.length===1?catalog?.assets.find(a=>a.id===route.assetIds[0]):undefined;
    onDexPrice(route&&asset?new URLSearchParams({poolAddress:asset.contractAddress,dexSymbol:route.symbol,dexNetwork:asset.dexNetwork,
      futuresVenue:route.futuresVenue,futuresSymbol:route.futuresSymbol,futuresDivisor:String(route.futuresDivisor),rangeHours:String(query.rangeHours)}):undefined);
  };
  const points=payload?.points||[];
  const openReference=points.length>=30?quantile(points.map(p=>p.openSpreadPct),.9):null;
  const closeReference=points.length>=30?quantile(points.map(p=>p.closeSpreadPct),.5):null;
  const last=points[points.length-1];
  const gapStats=useMemo(()=>bookGapStats(payload?.points||[]),[payload]);
  const gapJudgment=gapStats.difference===null ? "暂无可比较数据"
    : Math.abs(gapStats.difference)<0.0005 ? "最近盘口差与区间均值基本相同"
    : `最近比均值${gapStats.difference>0?"宽":"窄"} ${pp(Math.abs(gapStats.difference))}`;
  const option=useMemo(()=>{
    const rows=(payload?.points||[]).flatMap((p,i,all)=>i&&p.timestamp-all[i-1].timestamp>payload!.bucketSeconds*1000
      ?[{timestamp:all[i-1].timestamp+payload!.bucketSeconds*1000,openSpreadPct:null,closeSpreadPct:null},p]:[p]);
    const makeLine=(side:"open"|"close")=>{
      const isOpen=side==="open",field=isOpen?"openSpreadPct":"closeSpreadPct",level=isOpen?openReference:closeReference;
      return {name:isOpen?"开仓差价":"平仓差价",type:"line",showSymbol:false,connectNulls:false,
        lineStyle:{width:2,color:isOpen?"#1677ff":"#d97706"},itemStyle:{color:isOpen?"#1677ff":"#d97706"},
        data:rows.map(p=>[p.timestamp,p[field]]),
        markLine:{symbol:"none",silent:true,label:{show:true,position:isOpen?"insideStartTop":"insideEndBottom",formatter:"{b}"},data:[
          {yAxis:0,label:{show:false},lineStyle:{color:"#c3cbc7",width:1,type:"dotted"}},
          ...(references&&level!==null?[{yAxis:level,name:`${isOpen?"开仓 P90":"平仓 P50"} ${pct(level)}`,lineStyle:{color:isOpen?"#1677ff":"#d97706",type:"dashed",width:1.5}}]:[])]}};
    };
    const byTime=new Map((payload?.points||[]).map(p=>[p.timestamp,p]));
    return {animation:false,grid:{left:62,right:24,top:36,bottom:65},
      tooltip:{trigger:"axis",axisPointer:{type:"cross"},formatter:(items:unknown)=>{
        const first=(items as Array<{value:[number,number|null]}>)[0];const p=first&&byTime.get(first.value[0]);if(!p)return "无记录";
        return [`<b>${formatTime(p.timestamp)}</b>`,`开仓差价：${pct(p.openSpreadPct)}`,`平仓差价：${pct(p.closeSpreadPct)}`,`盘口差：${pp(bookGap(p))}`,
          `${escapeHtml(payload?.pair.leftLabel||"左侧")} 买 / 卖：${price(p.leftBid)} / ${price(p.leftAsk)}`,
          `${escapeHtml(payload?.pair.rightLabel||"右侧")} 买 / 卖：${price(p.rightBid)} / ${price(p.rightAsk)}`,"买一卖一参考 · 源报价时间未知"].join("<br/>");}},
      xAxis:{type:"time",min:payload?.requestedStart,max:payload?.requestedEnd,splitNumber:4,
        axisLabel:{hideOverlap:true,formatter:(v:number)=>payload&&payload.requestedEnd-payload.requestedStart<=86400000?formatTime(v).slice(-8,-3):formatTime(v).slice(0,-3)}},
      yAxis:{type:"value",scale:true,name:"%",axisLabel:{formatter:(v:number)=>v.toFixed(2)},splitLine:{lineStyle:{color:"#eff1f0"}}},
      dataZoom:[{type:"inside",filterMode:"none"},{type:"slider",height:20,bottom:14}],
      series:lines==="both"?[makeLine("open"),makeLine("close")]:[makeLine(lines)]};
  },[payload,references,lines,openReference,closeReference]);
  return <div className="book-spread-history">
    <Card size="small">
      <div className="book-history-toolbar"><Segmented aria-label="套利类型" value={query.pairType} options={[{label:"FF · 合约—合约",value:"FF"},{label:"SF · 现货—合约",value:"SF"}]}
        onChange={value=>change({pairType:value as PairType,leftVenue:value==="FF"&&["okxdex","pancakeswapv3"].includes(query.leftVenue)?"aster":query.leftVenue,leftDivisor:1})} />
        <Button size="small" loading={catalogLoading} onClick={()=>void loadCatalog()}>刷新卡片</Button></div>
      {catalogError&&<Alert type="info" message={catalogError} />}
      <div className="dex-history-card-list">{catalog?.bookCards?.filter(c=>c.type===query.pairType).map(c=><Button key={c.id||`${c.type}:${c.symbol}:${c.buyExchange}:${c.sellExchange}`} size="small" type={selected?.id===c.id?"primary":"default"} onClick={()=>choose(c)}>
        {c.symbol} · {label(c.buyExchange)} / {label(c.sellExchange)}</Button>)}</div>
      <form onSubmit={e=>{e.preventDefault();void run(query);}}>
        <div className="book-history-legs">
          <div><Typography.Text strong>左侧 · 买入{query.pairType==="FF"?"合约":"现货"}</Typography.Text>
            <Select aria-label="左侧交易所" value={query.leftVenue} options={query.pairType==="SF"?SPOT_VENUES:VENUES} onChange={v=>change({leftVenue:v,leftDivisor:1})} />
            <Input aria-label="左侧交易对" value={query.leftSymbol} placeholder="币名或交易对，例如 STONKS" onChange={e=>change({leftSymbol:e.target.value.toUpperCase(),rightSymbol:query.rightSymbol===query.leftSymbol?e.target.value.toUpperCase():query.rightSymbol,leftDivisor:1})} /></div>
          <div><Typography.Text strong>右侧 · 卖出合约</Typography.Text>
            <Select aria-label="右侧交易所" value={query.rightVenue} options={VENUES} onChange={v=>change({rightVenue:v,rightDivisor:1})} />
            <Input aria-label="右侧交易对" value={query.rightSymbol} placeholder="币名或交易对，例如 STONKS" onChange={e=>change({rightSymbol:e.target.value.toUpperCase(),rightDivisor:1})} /></div>
        </div>
        <div className="book-history-toolbar">
          <Segmented aria-label="历史区间" options={RANGES} value={query.rangeHours} onChange={v=>{const q={...query,rangeHours:Number(v)};if(canQuery)void run(q);else change(q);}} />
          <Space><Button type="primary" htmlType="submit" disabled={!canQuery} loading={loading}>查询差价</Button>
            <Button disabled={!canQuery} onClick={()=>navigator.clipboard.writeText(`${location.origin}${location.pathname}?${params(query)}`).then(()=>message.success("查询链接已复制"),()=>message.error("复制失败"))}>复制链接</Button></Space>
        </div>
      </form>
      <Collapse ghost size="small" items={[{key:"more",label:"卡片复制 / 每币换算 / 最近查询",children:<>
        <Space.Compact style={{width:"100%"}}><Input.TextArea aria-label="卡片复制内容" rows={2} value={copy} onChange={e=>setCopy(e.target.value)} placeholder="ASTRO-QUICK-COPY: {...}" /><Button disabled={!copy.trim()} onClick={parseCopy}>识别查询</Button></Space.Compact>
        <div className="book-history-divisors"><span>左侧价格 ÷ <InputNumber aria-label="左侧每币换算" min={1e-12} max={1e12} value={query.leftDivisor} onChange={v=>change({leftDivisor:v||1})} /></span>
          <span>右侧价格 ÷ <InputNumber aria-label="右侧每币换算" min={1e-12} max={1e12} value={query.rightDivisor} onChange={v=>change({rightDivisor:v||1})} /></span></div>
        <Typography.Text type="secondary">例如 1000 币合约价格需除以 1000。选择当前卡片时优先带入已有映射；手动输入请核对。</Typography.Text>
        <div className="dex-history-recents">{saved.map((q,i)=><Button size="small" key={i} onClick={()=>{setSelected(null);void run(q);}}>{q.leftSymbol} {q.pairType} · {label(q.leftVenue)} / {label(q.rightVenue)}</Button>)}</div>
      </>}]} />
    </Card>
    {error&&<Alert showIcon type="error" message={error} />}
    {loading&&<div className="dex-history-empty"><Spin size="small" /><span>正在读取开仓、平仓盘口历史…</span></div>}
    {!loading&&!payload&&!error&&<div className="dex-history-empty">选择卡片或输入交易对，查看开仓与平仓两条曲线</div>}
    {!loading&&payload&&payload.status!=="ok"&&<Alert showIcon type="info" message={payload.message}
      description={query.pairType==="SF"?<Space wrap><span>现货和合约分开核对；没有记录的历史保留为空。</span>{["okxdex","pancakeswapv3"].includes(query.leftVenue)&&<Button size="small" onClick={showDex}>查看 DEX 主池价格</Button>}</Space>:"可以更换时间范围或检查交易所与合约代码。"} />}
    {!loading&&payload?.status==="ok"&&<Card className="dex-history-result-card">
      <div className="book-history-toolbar"><Typography.Text strong>{query.leftSymbol} {query.pairType} · {payload.pair.leftLabel} / {payload.pair.rightLabel}</Typography.Text>
        <Typography.Text type="secondary">第三方盘口历史 · PERPDEXLIST</Typography.Text></div>
      <div className="book-history-stats">
        <div className="is-open"><span>最近开仓差价</span><strong>{pct(last?.openSpreadPct)}</strong><small>买左侧卖价 · 卖右侧买价</small></div>
        <div className="is-close"><span>最近平仓差价</span><strong>{pct(last?.closeSpreadPct)}</strong><small>卖左侧买价 · 买右侧卖价</small></div>
        <div className="is-book-gap"><span>平均盘口差</span><strong>{gapStats.average===null?"—":gapStats.average.toFixed(3)}<em> 个百分点</em></strong>
          <small>最大盘口差 {pp(gapStats.maximum)}</small>
          <small>最小盘口差 {pp(gapStats.minimum)}</small>
          <small>最近盘口差 {pp(gapStats.latest)}</small>
          <small className={gapStats.difference!==null&&gapStats.difference>=0.0005?"is-wider":""}>{gapJudgment}</small></div>
        <div><span>所选区间覆盖</span><strong>{payload.stats?.coveragePct.toFixed(1)}%</strong><small>{payload.stats?.pointCount} / {payload.stats?.expectedPointCount} 个点</small></div>
      </div>
      <div className="book-history-toolbar"><Segmented aria-label="显示曲线" value={lines} onChange={v=>setLines(v as typeof lines)} options={[{label:"两条曲线",value:"both"},{label:"开仓",value:"open"},{label:"平仓",value:"close"}]} />
        <Space><span>历史分位参考</span><Switch size="small" checked={references} disabled={points.length<30} onChange={setReferences} /></Space></div>
      <div ref={chartHost} className="dex-history-chart"><AppChart ref={chartRef} option={option} style={{height:410,width:"100%"}} notMerge /></div>
      <div className="dex-history-footnote">蓝色：开仓差价；橙色：平仓差价。平仓差价越低，表示两腿平仓价格关系越有利；它不是本次持仓收益。</div>
      <div className="dex-history-footnote">{last?`记录至 ${formatTime(last.timestamp)} · `:""}买一卖一参考，未核实 20U 深度、费用及两腿原始报价时间；仅辅助查看。</div>
      {payload.stats&&payload.stats.coveragePct<99&&<Alert type="info" message="部分时段没有记录，图中保留空白；不使用普通价格或插值补齐。" />}
      {!!(payload.quality?.invalidBars||payload.quality?.conflictingBars)&&<Alert type="warning" message="部分报价异常或相互冲突，已剔除相应历史点。" />}
      <Collapse ghost size="small" items={[{key:"details",label:"覆盖范围 / 计算口径",children:<>
        <p>实际记录：{payload.stats?.firstTimestamp?formatTime(payload.stats.firstTimestamp):"—"} 至 {last?formatTime(last.timestamp):"—"}；每 {payload.bucketSeconds} 秒一个采样桶，区间内缺 {payload.stats?.internalMissingBuckets} 桶。</p>
        <p>开仓（%）：{payload.definition?.open}<br />平仓（%）：{payload.definition?.close}</p>
        <p>盘口差 = 同一时刻的平仓差价 − 开仓差价。例如开仓 1%、平仓 2%，盘口差为 1 个百分点。
          平均盘口差取所选区间 {gapStats.samples} 个有效配对点的算术平均；缺失数据不补零，不用不同时间的开平仓点相减。它描述两条盘口曲线的距离，不等于持仓收益或扣费后的实际损耗。</p>
        <p>开仓参考取开仓曲线 P90；平仓参考取平仓曲线 P50。两者分别统计，未扣手续费、资金费或滑点，也不证明差价必然收敛；不修改卡片阈值。有效点不足 30 个时不画分位参考线。</p>
        <p>两条曲线均按本地 Astro 对称百分比换算，来自买卖价采样桶的收盘值。源盘口可能陈旧，短暂尖峰可能未被记录。资产身份仍须以实际市场为准。</p>
        <a href={payload.sourceUrl} target="_blank" rel="noreferrer">查看来源网站</a>
      </>}]} />
    </Card>}
  </div>;
}
