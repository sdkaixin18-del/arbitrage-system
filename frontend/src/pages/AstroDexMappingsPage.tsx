import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Input, Modal, Select, Space, Table, Tabs, Tag, Typography, message } from "antd";
import { cryptoApi as api } from "../api/crypto";
import AstroChainLabel, { ASTRO_CHAINS } from "../components/AstroChainLabel";

type Asset = {exchange?: string; symbol: string; chainIndex: string; chainLabel?: string; contractAddress: string;
  sourceExchanges?: string[]; targetExchanges?: string[]; maxOpenSpreadPct?: number | null; autoCreateEligible?: boolean};
const key = (row: Asset) => row.contractAddress ? `${row.chainIndex}:${row.chainIndex === "501" ? row.contractAddress : row.contractAddress.toLowerCase()}` : row.symbol;
const networks: Record<string,string> = Object.fromEntries(ASTRO_CHAINS.map(chain => [chain.chainId, chain.network]));
const chains = ASTRO_CHAINS.map(chain => ({value:chain.chainId,label:chain.label}));
function shared(rows: Asset[]): Asset[] {
  const grouped = new Map<string,Asset>();
  for (const row of rows) {
    const id=key(row); const old=grouped.get(id);
    grouped.set(id,{...(old || row),sourceExchanges:[...new Set([...(old?.sourceExchanges || []),row.exchange || "okxdex"])],
      maxOpenSpreadPct:Math.max(old?.maxOpenSpreadPct || 0,row.maxOpenSpreadPct || 0) || null});
  }
  return [...grouped.values()];
}
function historyLink(row: Asset) {
  return `/dex-history?${new URLSearchParams({dexNetwork:networks[row.chainIndex],dexSymbol:row.symbol,poolAddress:row.contractAddress})}`;
}

export default function AstroDexMappingsPage() {
  const queryClient = useQueryClient();
  const query = useQuery({queryKey:["astro-auto-card-status"],queryFn:api.astroAutoCardStatus,refetchInterval:5000});
  const [search,setSearch] = useState("");
  const [draft,setDraft] = useState<Asset | null>(null);
  const scanner = query.data?.spreadScanner;
  const saved = shared(scanner?.dexMappedAssets ?? []);
  const pending: Asset[] = shared(scanner?.autoCardRules?.sf.okxDexRoute?.unmappedItems ?? []);
  const incomplete = saved.filter(row => !row.chainIndex || !row.contractAddress);
  const filter = (rows: Asset[]) => rows.filter(row => `${row.symbol} ${row.contractAddress} ${row.chainLabel ?? ""}`.toLowerCase().includes(search.trim().toLowerCase()));
  const save = useMutation({mutationFn:api.confirmAstroDexMapping, onSuccess:data => {
    queryClient.setQueryData(["astro-auto-card-status"],data);setDraft(null);message.success("已保存共用链和合约映射");
  },onError:error => message.error(String(error))});
  const admin = query.data?.baseUrl && query.data.adminPrefix
    ? `${query.data.baseUrl.replace(/\/$/,"")}/${query.data.adminPrefix.replace(/^\//,"")}/dashboard` : undefined;
  const columns = [
    {title:"币种",dataIndex:"symbol",width:140},
    {title:"配置适用",render:() => <span>OKXDEX / PancakeSwap V3</span>,width:200},
    {title:"链 / 合约地址",render:(_:unknown,row:Asset) => <Space direction="vertical" size={2}>
      <Tag><AstroChainLabel chain={row.chainIndex} fallback={row.chainLabel || "待补全"} /></Tag><Typography.Text copyable={!!row.contractAddress}>{row.contractAddress || "旧记录只有币名"}</Typography.Text>
    </Space>},
    {title:"发现差价",render:(_:unknown,row:Asset) => row.maxOpenSpreadPct == null ? "—" : `${row.maxOpenSpreadPct.toFixed(2)}%`,width:110},
  ];
  const table = (rows:Asset[], editable:boolean) => <Table size="small" rowKey={key} dataSource={filter(rows)} scroll={{x:800}}
    pagination={{pageSize:10,showSizeChanger:false}} columns={[...columns, {title:"操作",width:180,
      render:(_:unknown,row:Asset) => <Space>{editable && <Button onClick={()=>setDraft({...row,exchange:row.exchange ?? "okxdex"})}>确认配置</Button>}
        {!!row.contractAddress && !!networks[row.chainIndex] && <Button href={historyLink(row)}>查看历史</Button>}</Space>}]} />;
  return <div className="astro-rules-page">
    <header className="astro-page-toolbar"><div><Typography.Title level={3}>DEX 配置</Typography.Title>
      <Typography.Text type="secondary">在 Astro 配置一次币种，登记链和合约地址；同链同地址由两家 DEX 共用。</Typography.Text></div>
      <Space><Button href="/dex-history">历史差价</Button>{admin && <Button href={admin} target="_blank">打开 Astro</Button>}<Button onClick={()=>query.refetch()}>刷新</Button></Space>
    </header>
    {query.isError && <Alert type="error" showIcon message="配置读取失败" description={String(query.error)} />}
    <section className="astro-rules-card">
      <Space wrap><Input.Search value={search} onChange={e=>setSearch(e.target.value)} placeholder="搜索币名 / 合约" allowClear />
        <Button onClick={()=>setDraft({exchange:"okxdex",symbol:"",chainIndex:"",contractAddress:""})}>登记已配置币种</Button>
      </Space>
      <Tabs items={[
        {key:"pending",label:`待配置 ${pending.length}`,children:table(pending,true)},
        {key:"configured",label:`已配置 ${saved.length-incomplete.length}`,children:table(saved.filter(row=>row.chainIndex && row.contractAddress),false)},
        {key:"incomplete",label:`待补全 ${incomplete.length}`,children:<><Alert type="info" showIcon message="保留了旧的币名确认记录；补全链和地址后才能作为完整映射。" />{table(incomplete,true)}</>},
      ]} />
    </section>
    <Modal title="确认 Astro 已配置该币种" open={!!draft} onCancel={()=>setDraft(null)} confirmLoading={save.isPending}
      okText="已配置，保存映射" onOk={()=>draft && save.mutate({exchange:draft.exchange ?? "okxdex",symbol:draft.symbol,chainIndex:draft.chainIndex,contractAddress:draft.contractAddress})}>
      <Typography.Paragraph>请先核对 Astro 中的配置。此处保存本地映射，不会替你配置钱包或下单。</Typography.Paragraph>
      {draft && <div className="astro-dex-config-form">
        <Typography.Text type="secondary">适用于 OKXDEX 和 PancakeSwap V3；两家的扫描开关仍分别控制。</Typography.Text>
        <label>币名<Input value={draft.symbol} onChange={e=>setDraft({...draft,symbol:e.target.value.toUpperCase()})} /></label>
        <label>链<Select placeholder="选择链" value={draft.chainIndex || undefined} options={chains} virtual={false} listHeight={280}
          optionRender={option=><AstroChainLabel chain={String(option.value)} />}
          labelRender={option=><AstroChainLabel chain={String(option.value)} />} onChange={value=>setDraft({...draft,chainIndex:value})} /></label>
        <label>合约地址<Input value={draft.contractAddress} onChange={e=>setDraft({...draft,contractAddress:e.target.value.trim()})} /></label>
      </div>}
    </Modal>
  </div>;
}
