import { Alert, Button, Space, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "../apiClient";

type Chain = { chain: string; depositEnabled?: boolean | null; withdrawEnabled?: boolean | null };
type Route = { symbol: string; exchange: string; status: string; message?: string; checkedAt?: string; updatedAt?: string; chains: Chain[] };
type Change = { chain: string; kind: string; before: boolean; after: boolean };
type Event = { id: number; symbol: string; exchange: string; changes: Change[]; createdAt: string; pushed: boolean; suppressed?: boolean; pushError?: string };
type Overview = { status: string; message?: string; items: Route[]; recentEvents: Event[]; intervalSeconds?: number };
const names: Record<string, string> = { bn: "Binance", by: "Bybit", gt: "Gate", okx: "OKX", bg: "Bitget", as: "Aster" };
const flag = (value?: boolean | null) => value === true ? "正常" : value === false ? "暂停" : "未知";
const stamp = (value?: string) => value ? new Date(value).toLocaleString() : "等待查询";

export default function TransferStatusWatch() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["fs-transfer-watch"], queryFn: () => request<Overview>("/api/fs/transfer-watch"), refetchInterval: 5000, retry: false });
  const refresh = useMutation({ mutationFn: () => request<Overview>("/api/fs/transfer-watch/refresh", { method: "POST" }), onSuccess: data => client.setQueryData(["fs-transfer-watch"], data) });
  return <div style={{ marginTop: 16 }}>
    <Space wrap>
      <Typography.Text strong>充提状态变化</Typography.Text>
      <Tag color="cyan">腾讯云查询 · 每 {query.data?.intervalSeconds ?? 30} 秒</Tag>
      <Tag>Bark 变化推送</Tag>
      <Button size="small" loading={refresh.isPending} onClick={() => refresh.mutate()}>检查充提</Button>
    </Space>
    <Typography.Paragraph type="secondary" style={{ marginTop: 8 }}>
      跟随上方监控币种与交易所，名单约 5 秒同步。逐链监控充值、提现暂停与恢复；首次查询和状态不变不推送。Bark 由本地服务转发，本地离线期间不即时推送。
    </Typography.Paragraph>
    {(query.isError || refresh.isError || query.data?.status === "error") && <Alert type="warning" showIcon message="充提状态暂不可用" description={query.data?.message || String(query.error || refresh.error)} />}
    {!query.data?.items.length && <Typography.Text type="secondary">等待监控名单同步；未添加币种时不查询。</Typography.Text>}
    {query.data?.items.map(route => <div key={`${route.exchange}:${route.symbol}`} style={{ padding: "8px 0", borderBottom: "1px solid #eef2f0" }}>
      <Space wrap>
        <Typography.Text strong>{route.symbol} · {names[route.exchange] || route.exchange}</Typography.Text>
        <Typography.Text type="secondary">数据时间 {stamp(route.updatedAt || route.checkedAt)}</Typography.Text>
        {route.status !== "ok" ? <Tag color="orange">未知：{route.message || "等待检查"}</Tag> : route.chains.length ? route.chains.map(chain =>
          <Tag key={chain.chain} color={chain.depositEnabled === false || chain.withdrawEnabled === false ? "red" : chain.depositEnabled === true && chain.withdrawEnabled === true ? "green" : "default"}>
            {chain.chain} · 充值{flag(chain.depositEnabled)} / 提现{flag(chain.withdrawEnabled)}
          </Tag>) : <Tag>未知：未返回链状态</Tag>}
      </Space>
    </div>)}
    {query.data?.recentEvents.slice(0,8).map(event => <div key={event.id} style={{ marginTop: 6 }}>
      <Space wrap><Typography.Text>{event.symbol} · {names[event.exchange] || event.exchange}</Typography.Text>
        {event.changes.map(change => <Tag key={`${change.chain}:${change.kind}`}>{change.chain} {change.kind === "depositEnabled" ? "充值" : "提现"}{change.after ? "恢复" : "暂停"}</Tag>)}
        <Tag color={event.pushed ? "green" : "default"}>{event.pushed ? "Bark 已推送" : event.suppressed ? "已停止" : "待推送"}</Tag>
        <Typography.Text type="secondary">{stamp(event.createdAt)}</Typography.Text>
      </Space>
    </div>)}
  </div>;
}
