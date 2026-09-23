import { ReloadOutlined, StockOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Empty,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import {
  type ExchangeAnnouncement,
  type ExchangeAnnouncementSource,
  type ExchangeAnnouncementsResponse
} from "../api";
import { messagingApi as api } from "../api/messaging";

const exchangeFilters = [
  { text: "Binance", value: "bn" },
  { text: "Bitget", value: "bg" },
  { text: "Bybit", value: "by" },
  { text: "Gate", value: "gate" },
  { text: "OKX", value: "okx" },
  { text: "Aster", value: "aster" },
  { text: "Hyperliquid", value: "hl" }
];

const CACHE_MS = 10 * 60 * 1000;
const GC_MS = 30 * 60 * 1000;
const STORAGE_MAX_AGE_MS = 6 * 60 * 60 * 1000;
const STORAGE_KEY = "exchange-announcements-cache-v2";
const STORAGE_VERSION = 17;
const queryKey = ["exchange-announcements", STORAGE_VERSION] as const;

const sourceAliases: Record<string, string> = {
  bn: "BN",
  bg: "BG",
  by: "BY",
  gate: "Gate",
  okx: "OKX",
  aster: "Aster",
  hl: "HL"
};

function statusColor(status?: string | null) {
  if (status === "loading") return "processing";
  if (status === "ok") return "green";
  if (status === "partial_error") return "orange";
  if (status === "error") return "red";
  if (status === "manual_only") return "blue";
  return "default";
}

function statusLabel(status?: string | null) {
  if (status === "loading") return "读取中";
  if (status === "ok") return "正常";
  if (status === "partial_error") return "部分异常";
  if (status === "error") return "异常";
  if (status === "not_configured") return "未配置";
  if (status === "manual_only") return "关闭";
  return status ?? "未配置";
}

function sourceTooltip(source: ExchangeAnnouncementSource) {
  const rawCount = source.raw_count ?? source.count;
  const freshness = source.last_success_at
    ? `上次成功 ${formatShortTime(source.last_success_at)}，年龄 ${source.age_seconds ?? "-"}秒`
    : "尚无成功读取";
  const speed = source.duration_ms == null ? "" : `，耗时 ${source.duration_ms}ms`;
  const next = source.next_refresh_seconds == null ? "" : `，${source.next_refresh_seconds}秒后刷新`;
  const failures = source.consecutive_failures
    ? `，连续失败 ${source.consecutive_failures} 次`
    : "";
  const error = source.status !== "ok" ? `；${source.message || "来源暂时不可用"}` : "";
  return `${source.exchange_name}：监控 ${source.count} 条，原始 ${rawCount} 条，过滤 ${source.expired_count ?? 0} 条；${freshness}${speed}${next}${failures}${error}`;
}

function briefMessage(data: ExchangeAnnouncementsResponse) {
  const rawCount = data.sources.reduce((total, source) => total + (source.raw_count ?? source.count), 0);
  const filteredCount = data.sources.reduce((total, source) => total + (source.expired_count ?? 0), 0);
  return `监控 ${data.items.length} 条公告｜原始 ${rawCount} 条｜过滤 ${filteredCount} 条`;
}

function pushTooltip(data?: ExchangeAnnouncementsResponse) {
  if (!data) return "推送状态读取中";
  if (data.push_status === "ok") return "Bark 推送通道已配置，交易所公告有新增时会推送。";
  return data.push_message || `Bark 状态：${statusLabel(data.bark_status)}`;
}

const beijingFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false
});

function parseBeijingDate(value: string | null) {
  if (!value) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;

  const hasTimeZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(trimmed);
  let normalized = trimmed;
  if (!hasTimeZone && /^\d{4}-\d{2}-\d{2}$/.test(trimmed)) {
    normalized = `${trimmed}T00:00:00+08:00`;
  } else if (!hasTimeZone && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(trimmed)) {
    normalized = `${trimmed.replace(" ", "T")}+08:00`;
  }

  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatTime(value: string | null) {
  const date = parseBeijingDate(value);
  if (!date) return "-";
  return beijingFormatter.format(date).replace(/\//g, "-");
}

function formatShortTime(value: string | null) {
  const formatted = formatTime(value);
  return formatted === "-" ? formatted : formatted.slice(5, 16);
}

function formatPublishedDate(value: string | null) {
  const formatted = formatTime(value);
  return formatted === "-" ? "日期未知" : formatted.slice(0, 10);
}

function formatPublishedDateTag(value: string | null) {
  const formatted = formatPublishedDate(value);
  return formatted === "日期未知" ? "日期未知" : formatted.slice(5);
}

function formatPublishedTime(value: string | null) {
  const formatted = formatTime(value);
  return formatted === "-" ? "发布时间未知" : formatted.slice(5);
}

function astroLinkageLabel(status?: string | null) {
  if (status === "waiting_market") return "等待市场";
  if (status === "market_opened") return "市场已开放";
  if (status === "registered") return "重点监控中";
  if (status === "direct_checking") return "API连续复核";
  if (status === "direct_rejected") return "直连暂未达标";
  if (status === "submitted_for_card_sync") return "已提交建卡";
  if (status === "card_created") return "已建卡";
  if (status === "not_applicable") return "不适用";
  return "待联动";
}

function astroLinkageColor(status?: string | null) {
  if (status === "card_created") return "green";
  if (status === "submitted_for_card_sync") return "cyan";
  if (status === "registered" || status === "direct_checking") return "blue";
  if (status === "direct_rejected") return "orange";
  if (status === "market_opened") return "geekblue";
  return "default";
}

function timelineTooltip(record: ExchangeAnnouncement) {
  const timeline = record.timeline;
  if (!timeline) return record.astro_linkage?.reason || "公告时间线尚未由后台同步";
  return (
    <Space direction="vertical" size={0}>
      <span>发布时间：{formatTime(timeline.publishedAt)}</span>
      <span>发现：{formatTime(timeline.detectedAt)}</span>
      <span>推送：{formatTime(timeline.pushedAt)}</span>
      <span>市场开放：{formatTime(timeline.marketOpenedAt)}</span>
      <span>Astro登记：{formatTime(timeline.astroRegisteredAt)}</span>
      <span>首次API复核：{formatTime(timeline.firstDirectCheckAt)}</span>
      <span>{timeline.astroReason || record.astro_linkage?.reason || "-"}</span>
    </Space>
  );
}

function countdownText(eventAt: string | null, now: number, hasOccurred: boolean) {
  const targetDate = parseBeijingDate(eventAt);
  if (!targetDate) return "-";
  const target = targetDate.getTime();
  const seconds = Math.max(0, Math.floor((target - now) / 1000));
  if (hasOccurred || seconds === 0) return "已发生";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return `${hours.toString().padStart(2, "0")}:${minutes.toString().padStart(2, "0")}:${rest
    .toString()
    .padStart(2, "0")}`;
}

function normalizeResponse(data: ExchangeAnnouncementsResponse): ExchangeAnnouncementsResponse {
  const seen = new Set<string>();
  return {
    ...data,
    push_logs: (data.push_logs ?? []).filter((log) => {
      const key = `${log.link ?? ""}|${log.body}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
  };
}

function readStoredAnnouncements() {
  if (typeof window === "undefined") return undefined;
  try {
    const cached = window.localStorage.getItem(STORAGE_KEY);
    if (!cached) return undefined;
    const parsed = JSON.parse(cached) as {
      version?: number;
      savedAt: number;
      data: ExchangeAnnouncementsResponse;
    };
    if (parsed.version !== STORAGE_VERSION) return undefined;
    if (!parsed.savedAt || Date.now() - parsed.savedAt > STORAGE_MAX_AGE_MS) return undefined;
    if (!parsed.data?.push_status || !Array.isArray(parsed.data?.push_logs)) return undefined;
    return { ...parsed, data: normalizeResponse(parsed.data) };
  } catch {
    return undefined;
  }
}

function writeStoredAnnouncements(data: ExchangeAnnouncementsResponse) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({ version: STORAGE_VERSION, savedAt: Date.now(), data: normalizeResponse(data) })
  );
}

function originalUrl(record: ExchangeAnnouncement) {
  return record.announcements[0]?.url ?? "";
}

function astroFundingUrl(symbol: string) {
  return `https://funding-v2.astro-btc.xyz/?coin=${encodeURIComponent(symbol)}`;
}

function renderSymbol(
  symbol: string,
  assetType?: string | null,
  assetLabel?: string | null,
  strong = false
) {
  const symbols = symbol
    .split("/")
    .map((item) => item.trim())
    .filter(Boolean);
  return (
    <Space wrap size={[4, 4]}>
      <span style={{ fontWeight: strong ? 600 : undefined, wordBreak: "break-word" }}>
        {(symbols.length ? symbols : [symbol]).map((item, index) => (
          <span key={`${item}-${index}`}>
            {index ? " / " : null}
            <Typography.Link
              href={astroFundingUrl(item)}
              target="_blank"
              rel="noreferrer"
              aria-label={`查看 ${item} 资金费率`}
            >
              {item}
            </Typography.Link>
          </span>
        ))}
      </span>
      {assetType === "stock" ? (
        <Tag icon={<StockOutlined />} color="gold">
          {assetLabel || "股票"}
        </Tag>
      ) : null}
    </Space>
  );
}

function buildColumns(now: number): ColumnsType<ExchangeAnnouncement> {
  return [
    {
      title: "标的",
      dataIndex: "symbol",
      width: 150,
      fixed: "left",
      render: (symbol: string, record) => renderSymbol(symbol, record.asset_type, record.asset_label, true)
    },
    {
      title: "交易所",
      dataIndex: "exchange_names",
      width: 120,
      filters: exchangeFilters,
      onFilter: (value, record) => record.exchange_codes.includes(String(value)),
      render: (names: string[]) => (
        <Space wrap size={[0, 4]}>
          {names.map((name) => (
            <Tag key={name}>{name}</Tag>
          ))}
        </Space>
      )
    },
    {
      title: "类型",
      dataIndex: "market_label",
      width: 105,
      filters: [
        { text: "现货", value: "spot" },
        { text: "USDT合约", value: "contract" },
        { text: "币本位合约", value: "contract_usd" },
        { text: "USDC合约", value: "contract_usdc" },
        { text: "杠杆/借币", value: "margin_loan" },
        { text: "未分类", value: "unknown" }
      ],
      onFilter: (value, record) => record.announcements.some((item) => item.market_type === value),
      render: (label: string) => (
        <Tag color={label.includes("合约") ? "blue" : label.includes("杠杆") ? "purple" : "green"}>{label}</Tag>
      )
    },
    {
      title: "动作",
      dataIndex: "action_label",
      width: 72,
      filters: [
        { text: "上架", value: "listing" },
        { text: "下架", value: "delisting" },
        { text: "暂停交易", value: "suspension" },
        { text: "恢复交易", value: "resumption" },
        { text: "改名/迁移", value: "migration" },
        { text: "参数变更", value: "parameter_change" }
      ],
      onFilter: (value, record) => record.announcements.some((item) => item.action === value),
      render: (label: string) => <Tag color={label.includes("下架") ? "red" : "geekblue"}>{label}</Tag>
    },
    {
      title: "新闻发布时间（北京时间）",
      key: "published_at",
      width: 205,
      sorter: (left, right) => {
        const leftTime = parseBeijingDate(left.latest_published_at)?.getTime() ?? 0;
        const rightTime = parseBeijingDate(right.latest_published_at)?.getTime() ?? 0;
        return leftTime - rightTime;
      },
      defaultSortOrder: "descend",
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          {record.announcements.map((detail) => (
            <Typography.Text key={`${detail.exchange}-${detail.url}-published`}>
              {record.announcements.length > 1 ? `${detail.exchange_name} ` : ""}
              {formatPublishedTime(detail.published_at)}
            </Typography.Text>
          ))}
        </Space>
      )
    },
    {
      title: "北京时间 / 倒计时",
      dataIndex: "event_at",
      width: 205,
      render: (_, record) => (
        <Space direction="vertical" size={0}>
          {record.announcements.map((detail) => (
            <Typography.Link
              key={`${detail.exchange}-${detail.url}`}
              href={detail.url}
              target="_blank"
              rel="noreferrer"
              strong={!detail.has_occurred}
            >
              {record.announcements.length > 1 ? `${detail.exchange_name} ` : ""}
              {formatShortTime(detail.event_at)} · {countdownText(detail.event_at, now, detail.has_occurred)}
            </Typography.Link>
          ))}
        </Space>
      )
    },
    {
      title: "Astro 联动",
      key: "astro_linkage",
      width: 160,
      render: (_, record) => {
        const status = record.timeline?.astroStatus || record.astro_linkage?.status;
        const routes = record.astro_linkage?.routeCount ?? 0;
        return (
          <Tooltip title={timelineTooltip(record)}>
            <Space direction="vertical" size={0}>
              <Tag color={astroLinkageColor(status)}>{astroLinkageLabel(status)}</Tag>
              {routes > 0 ? (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {routes} 条直连路线
                </Typography.Text>
              ) : null}
            </Space>
          </Tooltip>
        );
      }
    },
    {
      title: "标题",
      dataIndex: "title",
      width: 360,
      render: (_, record) => (
        <div className="exchange-announcement-title">
          <Tooltip
            title={
              record.latest_published_at
                ? `${formatPublishedDate(record.latest_published_at)} · 公告发布日期（北京时间）`
                : "公告来源未返回发布日期"
            }
          >
            <Tag color={record.latest_published_at ? "blue" : "default"}>
              {formatPublishedDateTag(record.latest_published_at)}
            </Tag>
          </Tooltip>
          <Typography.Link href={originalUrl(record)} target="_blank" rel="noreferrer">
            {record.title}
          </Typography.Link>
        </div>
      )
    }
  ];
}

export default function ExchangeAnnouncementsPage() {
  const [now, setNow] = useState(() => Date.now());
  const queryClient = useQueryClient();
  const [storedAnnouncements] = useState(readStoredAnnouncements);
  const query = useQuery({
    queryKey,
    queryFn: () => api.exchangeAnnouncements(),
    initialData: storedAnnouncements?.data,
    initialDataUpdatedAt: storedAnnouncements?.savedAt,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: 30_000,
    staleTime: CACHE_MS,
    gcTime: GC_MS
  });
  const refreshMutation = useMutation({
    mutationFn: () => api.exchangeAnnouncements(true),
    onSuccess: (data) => {
      writeStoredAnnouncements(data);
      queryClient.setQueryData(queryKey, data);
    },
    onError: (error) => message.error(String(error))
  });
  const pushMutation = useMutation({
    mutationFn: api.pushExchangeAnnouncements,
    onSuccess: (result) => {
      message.info(result.message);
      queryClient.invalidateQueries({ queryKey });
    },
    onError: (error) => message.error(String(error))
  });
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (query.data) writeStoredAnnouncements(query.data);
  }, [query.data]);

  const data = query.data ? normalizeResponse(query.data) : undefined;
  const currentStatus = query.isLoading && !data ? "loading" : data?.source_status ?? data?.status;

  return (
    <main className="page exchange-page">
      <div className="page-header exchange-page-header">
        <img className="exchange-page-logo" src="/exchange-announcement-logo.svg" alt="" />
        <div className="exchange-heading">
          <Space align="center" size={8}>
            <Typography.Title level={3}>交易所新闻监控</Typography.Title>
            <Tag className="exchange-status-pill" color={statusColor(currentStatus)}>
              {statusLabel(currentStatus)}
            </Tag>
          </Space>
          <Typography.Text className="exchange-subtitle" type="secondary">
            Binance / Bitget / Bybit / Gate / OKX / Aster｜USDT 合约与现货｜上架与下架
          </Typography.Text>
        </div>
        <Space className="exchange-header-actions">
          <Tooltip title={pushTooltip(data)}>
            <Tag color={statusColor(data?.push_status ?? "loading")}>
              推送 {statusLabel(data?.push_status ?? "loading")}
            </Tag>
          </Tooltip>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            loading={query.isFetching || refreshMutation.isPending}
            onClick={() => refreshMutation.mutate()}
          >
            刷新
          </Button>
          <Button size="small" loading={pushMutation.isPending} onClick={() => pushMutation.mutate()}>
            推送新增
          </Button>
        </Space>
      </div>

      {query.isError ? (
        <Alert type="error" showIcon message="公告接口暂时不可用" description={String(query.error)} />
      ) : null}

      {query.isLoading && !data ? (
        <div className="panel exchange-loading-panel">
          <Spin size="small" />
          <Typography.Text type="secondary">正在读取交易所公告</Typography.Text>
        </div>
      ) : null}

      {data ? (
        <>
          {(data.sources.some((source) => source.stale || source.status !== "ok")
            || (data.announcement_changes?.length ?? 0) > 0) ? (
            <Space direction="vertical" size={8} style={{ width: "100%", marginBottom: 12 }}>
              {data.sources.some((source) => source.stale || source.status !== "ok") ? (
                <Alert
                  type="warning"
                  showIcon
                  message="公告来源新鲜度告警"
                  description={data.sources
                    .filter((source) => source.stale || source.status !== "ok")
                    .map((source) => `${source.exchange_name}：${source.message || "超过新鲜度阈值"}`)
                    .join("｜")}
                />
              ) : null}
              {(data.announcement_changes?.length ?? 0) > 0 ? (
                <Alert
                  type="info"
                  showIcon
                  message="官方公告变更"
                  description={data.announcement_changes?.slice(0, 3).map((change) => (
                    <div key={change.id}>
                      {formatShortTime(change.createdAt)} · {change.summary}
                    </div>
                  ))}
                />
              ) : null}
            </Space>
          ) : null}
          <div className="panel">
            <div className="panel-toolbar exchange-toolbar">
              <Typography.Text className="exchange-summary" type="secondary">
                {briefMessage(data)}｜{data.push_message ?? "交易所公告推送状态读取中。"}
              </Typography.Text>
              <div className="exchange-source-bar">
                {data.sources.map((source) => (
                  <Tooltip key={source.exchange} title={sourceTooltip(source)}>
                    <Tag
                      className="exchange-source-pill"
                      color={source.stale ? "orange" : statusColor(source.status)}
                    >
                      {sourceAliases[source.exchange] ?? source.exchange_name}{" "}
                      {source.status === "ok"
                        ? `${source.count}/${source.raw_count ?? source.count}`
                        : statusLabel(source.status)}
                    </Tag>
                  </Tooltip>
                ))}
              </div>
            </div>
            <Table
              size="small"
              rowKey={(record) => record.key}
              loading={query.isLoading}
              dataSource={data.items}
              columns={buildColumns(now)}
              locale={{ emptyText: <Empty description="暂无今日有效公告" /> }}
              pagination={{ pageSize: 30, showSizeChanger: false }}
              scroll={{ x: 1160 }}
            />
          </div>
        </>
      ) : null}
    </main>
  );
}
