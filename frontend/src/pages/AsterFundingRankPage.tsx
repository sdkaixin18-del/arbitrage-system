import { CopyOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Empty, Segmented, Space, Spin, Table, Tag, Tooltip, Typography, message } from "antd";
import { useState } from "react";
import AstroChainLabel from "../components/AstroChainLabel";
import { cryptoApi, type AsterFundingRankAddress, type AsterFundingRankRow } from "../api/crypto";

const formatTime = (value: string | null) => value
  ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })
  : "—";
const formatVolume = (value: number | undefined) => value === undefined
  ? "—" : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);

function CoinLink({ coin }: { coin: string }) {
  return <a className="aster-rank-coin-link"
    href={`https://funding-v2.astro-btc.xyz/?coin=${encodeURIComponent(coin)}`}
    target="_blank" rel="noopener noreferrer">{coin}</a>;
}

function CopyButton({ value, label, onCopy }: { value: string; label: string; onCopy: (value: string, label: string) => void }) {
  return <Tooltip title={`复制${label}`}>
    <Button type="text" size="small" icon={<CopyOutlined />} aria-label={`复制${label}`}
      onClick={() => onCopy(value, label)} />
  </Tooltip>;
}

function ChainAddress({ row, onCopy }: { row: AsterFundingRankAddress; onCopy: (value: string, label: string) => void }) {
  return <div className="aster-rank-chain">
    <AstroChainLabel chain={row.chainId} />
    <Tooltip title={row.address}><Typography.Text className="aster-rank-address">{row.address}</Typography.Text></Tooltip>
    <CopyButton value={row.address} label="合约地址" onCopy={onCopy} />
  </div>;
}

export default function AsterFundingRankPage() {
  const [limit, setLimit] = useState<10 | 20 | 50>(20);
  const [messageApi, contextHolder] = message.useMessage();
  const copy = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
      messageApi.success(`已复制${label}`);
    } catch {
      messageApi.error("复制失败，请检查浏览器剪贴板权限");
    }
  };
  const query = useQuery({
    queryKey: ["aster-funding-rank", limit],
    queryFn: () => cryptoApi.asterFundingRank(limit),
    refetchOnWindowFocus: false,
    refetchInterval: current => {
      const data = current.state.data;
      if (!data) return false;
      if (data.status !== "ready") return 5 * 60_000;
      const nextPublication = Date.parse(data.nextCutoff) + 5 * 60_000;
      return Math.max(60_000, nextPublication - Date.now());
    },
  });
  const data = query.data;
  const columns = [
    { title: "排名", key: "rank", width: 66, render: (_: unknown, _row: AsterFundingRankRow, index: number) => index + 1 },
    { title: "币种", key: "coin", width: 172, render: (_: unknown, row: AsterFundingRankRow) => <Space size={4}>
      <CoinLink coin={row.coin} />
      <CopyButton value={row.coin} label="币名" onCopy={copy} />
      {row.listedWithin24h && <Tag>新上市</Tag>}
    </Space> },
    { title: "24小时总费率", dataIndex: "totalRatePct", width: 142,
      render: (value: number) => <Typography.Text className="aster-rank-rate">+{value.toFixed(4)}%</Typography.Text> },
    { title: "24h成交额 (USDT)", dataIndex: "volume24hUsdt", width: 180,
      render: (value: number | undefined) => <Typography.Text className="aster-rank-volume">{formatVolume(value)}</Typography.Text> },
    { title: "结算次数", dataIndex: "settlementCount", width: 92 },
    { title: "链 / 合约地址", key: "addresses", render: (_: unknown, row: AsterFundingRankRow) =>
      <div className="aster-rank-chains">{row.addresses.map(address =>
        <ChainAddress key={`${address.chainId}:${address.address}`} row={address} onCopy={copy} />)}</div> },
  ];

  return <main className="aster-rank-page">
    {contextHolder}
    <header className="aster-rank-header">
      <div>
        <Typography.Title level={3}>Aster 24小时正费率榜</Typography.Title>
        <Typography.Text type="secondary">已配置链上地址的 Aster USDT 永续合约</Typography.Text>
      </div>
      <Segmented aria-label="榜单数量" value={limit}
        options={[{ label: "前10", value: 10 }, { label: "前20", value: 20 }, { label: "前50", value: 50 }]}
        onChange={value => setLimit(value as 10 | 20 | 50)} />
    </header>

    <div className="aster-rank-meta">
      <span>统计截点：{formatTime(data?.asOf ?? null)}</span>
      <span>生成时间：{formatTime(data?.generatedAt ?? null)}</span>
      <span>纳入合约：{data?.eligibleCount ?? "—"}</span>
      <span>正费率：{data?.rankedCount ?? "—"}</span>
    </div>

    {query.isError && <Alert type="error" showIcon message="榜单读取失败" description={String(query.error)} />}
    {data?.status === "delayed" && <Alert type="warning" showIcon message="本期榜单尚未完成，当前显示上一期" />}
    {data?.lastError && <Alert type="warning" showIcon message="本期数据核验未完成" description={data.lastError} />}
    {query.isLoading ? <div className="aster-rank-loading"><Spin /></div>
      : data?.status === "pending" ? <Empty description={data.running ? "正在生成首期榜单" : "等待首期榜单"} />
        : <>
          <div className="aster-rank-desktop"><Table<AsterFundingRankRow> rowKey="symbol" size="middle" columns={columns}
            dataSource={data?.rows ?? []} pagination={false} scroll={{ x: 1030 }}
            locale={{ emptyText: "本期没有正费率合约" }} /></div>
          <div className="aster-rank-mobile">
            {(data?.rows ?? []).map((row, index) => <div className="aster-rank-mobile-row" key={row.symbol}>
              <div className="aster-rank-mobile-head">
                <span className="aster-rank-mobile-rank">{index + 1}</span>
                <CoinLink coin={row.coin} />
                <CopyButton value={row.coin} label="币名" onCopy={copy} />
                {row.listedWithin24h && <Tag>新上市</Tag>}
                <Typography.Text className="aster-rank-rate">+{row.totalRatePct.toFixed(4)}%</Typography.Text>
              </div>
              <div className="aster-rank-mobile-stats">
                <span>24h成交额 {formatVolume(row.volume24hUsdt)} USDT</span>
                <span>结算 {row.settlementCount} 次</span>
              </div>
              <div className="aster-rank-chains">{row.addresses.map(address =>
                <ChainAddress key={`${address.chainId}:${address.address}`} row={address} onCopy={copy} />)}</div>
            </div>)}
            {!data?.rows.length && <Empty description="本期没有正费率合约" />}
          </div>
        </>}
  </main>;
}
