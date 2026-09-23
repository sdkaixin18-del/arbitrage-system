import { Link } from "react-router-dom";
import { Alert, Button, Empty, Segmented, Space, Table, Tooltip, Typography, message } from "antd";
import { PlayCircleOutlined, ReloadOutlined, SettingOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useState } from "react";
import type { NetworkMessageItem, NetworkMessageSource, NetworkMessagesOverview } from "../api";
import { messagingApi as api } from "../api/messaging";
import StatusTag from "../components/StatusTag";

const FONT_SIZE_STORAGE_KEY = "network-message-content-font-size";
const NETWORK_MESSAGES_CACHE_KEY = "network-messages-overview-cache-v2";
const DEFAULT_CONTENT_FONT_SIZE = "14px";
const NETWORK_MESSAGE_PREVIEW_LIMIT = 90;
const contentFontOptions = [
  { label: "小", value: "12px" },
  { label: "中", value: "14px" },
  { label: "大", value: "16px" },
  { label: "特大", value: "18px" }
];

interface NetworkMessagesCacheEntry {
  cachedAt: number;
  data: NetworkMessagesOverview;
}

const beijingFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false
});

function dateText(value?: string | null) {
  if (!value) return "-";
  const normalized = /([zZ]|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return "-";
  return beijingFormatter.format(date).replace(/\//g, "-");
}

function parseStockLabel(label: string) {
  const [name, ...codeParts] = label.split(" ");
  return { name: name || label, code: codeParts.join(" ") };
}

function normalizeSummaryText(value: string) {
  return value
    .replace(/!\[[^\]]*]\([^)]*\)/g, " ")
    .replace(/https?:\/\/\S+/gi, " ")
    .replace(/[【】\[\]]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function compactForCompare(value: string) {
  return normalizeSummaryText(value)
    .replace(/[^\p{L}\p{N}]/gu, "")
    .toLowerCase();
}

function stripLeadingSymbols(value: string) {
  return value.replace(/^(?:[\p{Emoji_Presentation}\p{Extended_Pictographic}]|\s)+/gu, "").trim();
}

function stripRepeatedTitle(content: string, title: string) {
  const cleaned = stripLeadingSymbols(normalizeSummaryText(content));
  const titlePrefix = stripLeadingSymbols(normalizeSummaryText(title)).replace(/[.。…]+$/, "").trim();
  if (titlePrefix && cleaned.startsWith(titlePrefix)) {
    return cleaned.slice(titlePrefix.length).replace(/^[\s:：,，。;；-]+/, "").trim() || cleaned;
  }

  const cleanedKey = compactForCompare(cleaned);
  const titleKey = compactForCompare(title).slice(0, 18);
  if (!titleKey || !cleanedKey.startsWith(titleKey)) return cleaned;

  return cleaned;
}

function networkMessageSummary(record: NetworkMessageItem) {
  if (record.image_text) {
    const imageSummary = normalizeSummaryText(record.image_text);
    return imageSummary.length > 150 ? `图片识别：${imageSummary.slice(0, 150)}...` : `图片识别：${imageSummary}`;
  }
  if (record.image_count && record.image_status !== "ok") {
    return record.image_message || `含 ${record.image_count} 张图片，图片识别暂不可用。`;
  }
  const stripped = stripRepeatedTitle(record.content || "", record.title || "");
  const base = stripped || normalizeSummaryText(record.content || record.title || "");
  const summary = base.replace(/^(?:摘要|概括|内容)[:：]\s*/, "").trim();
  if (!summary) return "暂无摘要";
  return summary.length > 150 ? `${summary.slice(0, 150)}...` : summary;
}

function readContentFontSize() {
  if (typeof window === "undefined") return DEFAULT_CONTENT_FONT_SIZE;
  const stored = window.localStorage.getItem(FONT_SIZE_STORAGE_KEY);
  if (stored && contentFontOptions.some((option) => option.value === stored)) {
    return stored;
  }
  return DEFAULT_CONTENT_FONT_SIZE;
}

function readNetworkMessagesCache() {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(NETWORK_MESSAGES_CACHE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as NetworkMessagesCacheEntry;
    if (parsed?.data && Array.isArray(parsed.data.items) && Array.isArray(parsed.data.sources)) {
      return parsed;
    }
  } catch {
    window.localStorage.removeItem(NETWORK_MESSAGES_CACHE_KEY);
  }
  return null;
}

function saveNetworkMessagesCache(data: NetworkMessagesOverview) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(NETWORK_MESSAGES_CACHE_KEY, JSON.stringify({ cachedAt: Date.now(), data }));
}

function networkMessageFullText(record: NetworkMessageItem) {
  const sections: string[] = [];
  if (record.content) {
    sections.push(record.content);
  }
  if (record.image_text) {
    sections.push(`图片识别：${record.image_text}`);
  } else if (record.image_count && record.image_status !== "ok") {
    sections.push(record.image_message || `含 ${record.image_count} 张图片，图片识别暂不可用。`);
  }
  if (!sections.length) {
    sections.push(networkMessageSummary(record));
  }
  return sections.join("\n\n");
}

function shouldShowNetworkExpand(record: NetworkMessageItem) {
  if (!record.image_text && record.image_count && record.image_status !== "ok") {
    return false;
  }
  return normalizeSummaryText(networkMessageFullText(record)).length > NETWORK_MESSAGE_PREVIEW_LIMIT;
}

function SourceStrip({ sources }: { sources: NetworkMessageSource[] }) {
  return (
    <div className="network-source-strip">
      {sources.map((source) => {
        const messageText = source.message || "暂无状态说明";
        const updatedText = dateText(source.updated_at);
        return (
          <Tooltip
            key={source.source_type}
            placement="topLeft"
            title={
              <div className="network-source-tooltip">
                <div>{messageText}</div>
                <div>更新时间：{updatedText}</div>
              </div>
            }
          >
            <div className="network-source">
              <Typography.Text className="network-source-name" strong>
                {source.source_name}
              </Typography.Text>
              <StatusTag status={source.status} />
              <Typography.Text className="network-source-message" type="secondary">
                {messageText}
              </Typography.Text>
              <Typography.Text className="network-source-time" type="secondary">
                {updatedText}
              </Typography.Text>
            </div>
          </Tooltip>
        );
      })}
    </div>
  );
}

export function NetworkMessagesPanel({ embedded = false }: { embedded?: boolean }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("全部");
  const [selectedStock, setSelectedStock] = useState("all");
  const [expandedItems, setExpandedItems] = useState<Set<string>>(() => new Set());
  const [contentFontSize, setContentFontSize] = useState(readContentFontSize);
  const cachedOverview = useMemo(readNetworkMessagesCache, []);
  const overview = useQuery({
    queryKey: ["network-messages"],
    queryFn: () => api.networkMessages(),
    staleTime: 10 * 60 * 1000,
    gcTime: 30 * 60 * 1000,
    initialData: cachedOverview?.data,
    initialDataUpdatedAt: cachedOverview?.cachedAt,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    placeholderData: (previousData) => previousData
  });
  const forceRefresh = useMutation({
    mutationFn: () => api.networkMessages(true),
    onSuccess: (data) => {
      queryClient.setQueryData(["network-messages"], data);
      saveNetworkMessagesCache(data);
      queryClient.invalidateQueries({ queryKey: ["xueqiu"] });
    },
    onError: (error) => message.error(String(error))
  });

  const refresh = () => {
    forceRefresh.mutate();
  };
  const crawlHome = useMutation({
    mutationFn: api.crawlNetworkHomeFeed,
    onSuccess: (result) => {
      message.info(result.message ?? result.status);
      refresh();
    },
    onError: (error) => message.error(String(error))
  });
  const crawlZsxq = useMutation({
    mutationFn: api.crawlNetworkZsxq,
    onSuccess: (result) => {
      message.info(result.message ?? result.status);
      refresh();
    },
    onError: (error) => message.error(String(error))
  });

  const filteredItems = useMemo(() => {
    const items = overview.data?.items ?? [];
    if (filter === "雪球发言") return items.filter((item) => item.source_type === "xueqiu" && item.message_type === "发言");
    if (filter === "星球新闻") return items.filter((item) => item.source_type === "zsxq");
    return items;
  }, [filter, overview.data?.items]);

  const stockGroups = useMemo(() => {
    const groups = new Map<
      string,
      { label: string; name: string; code: string; count: number; zsxqCount: number; xueqiuCount: number; latestAt: string | null }
    >();
    filteredItems.forEach((item) => {
      const labels = item.matched_stocks?.length ? item.matched_stocks : ["未分组"];
      labels.forEach((label) => {
        const parsed = parseStockLabel(label);
        const group = groups.get(label) ?? {
          label,
          name: parsed.name,
          code: parsed.code,
          count: 0,
          zsxqCount: 0,
          xueqiuCount: 0,
          latestAt: null
        };
        group.count += 1;
        if (item.source_type === "zsxq") group.zsxqCount += 1;
        if (item.source_type === "xueqiu") group.xueqiuCount += 1;
        if (!group.latestAt || new Date(item.created_at).getTime() > new Date(group.latestAt).getTime()) {
          group.latestAt = item.created_at;
        }
        groups.set(label, group);
      });
    });
    return Array.from(groups.values()).sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
  }, [filteredItems]);

  useEffect(() => {
    if (selectedStock !== "all" && !stockGroups.some((group) => group.label === selectedStock)) {
      setSelectedStock("all");
    }
  }, [selectedStock, stockGroups]);

  useEffect(() => {
    window.localStorage.setItem(FONT_SIZE_STORAGE_KEY, contentFontSize);
  }, [contentFontSize]);

  useEffect(() => {
    if (overview.data && !overview.isPlaceholderData && overview.dataUpdatedAt !== cachedOverview?.cachedAt) {
      saveNetworkMessagesCache(overview.data);
    }
  }, [cachedOverview?.cachedAt, overview.data, overview.dataUpdatedAt, overview.isPlaceholderData]);

  const visibleItems = useMemo(() => {
    if (selectedStock === "all") return filteredItems;
    return filteredItems.filter((item) => item.matched_stocks?.includes(selectedStock));
  }, [filteredItems, selectedStock]);

  const allGroupCounts = useMemo(
    () => ({
      zsxq: filteredItems.filter((item) => item.source_type === "zsxq").length,
      xueqiu: filteredItems.filter((item) => item.source_type === "xueqiu").length
    }),
    [filteredItems]
  );

  const toggleExpandedItem = (id: string) => {
    setExpandedItems((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const columns: ColumnsType<NetworkMessageItem> = [
    { title: "时间", dataIndex: "created_at", width: 96, render: dateText },
    {
      title: "股票",
      dataIndex: "matched_stocks",
      width: 128,
      render: (value: string[]) => (
        <div className="network-stock-cell">
          <img src="/watchlist-stock-logo.svg" alt="" />
          {(value?.length ? value : ["未分组"]).map((label) => {
            const stock = parseStockLabel(label);
            return (
              <span className="network-stock-cell-text" key={label}>
                <span>{stock.name}</span>
                {stock.code ? <small>{stock.code}</small> : null}
              </span>
            );
          })}
        </div>
      )
    },
    {
      title: "来源",
      dataIndex: "message_type",
      width: 112,
      render: (value, record) => (
        <div className="network-source-type">
          <span className={`network-source-dot ${record.source_type === "zsxq" ? "is-zsxq" : "is-xueqiu"}`} />
          <span className="network-source-primary">{record.source_name}</span>
          <span className="network-source-secondary">{value}</span>
        </div>
      )
    },
    {
      title: "信息",
      dataIndex: "title",
      render: (_, record) => {
        const canExpand = shouldShowNetworkExpand(record);
        const expanded = canExpand && expandedItems.has(record.id);
        return (
          <div className="network-message-main">
            {record.link ? (
              <Typography.Link className="network-message-title" href={record.link} target="_blank" rel="noreferrer">
                {record.title}
              </Typography.Link>
            ) : (
              <Typography.Text className="network-message-title" strong>
                {record.title}
              </Typography.Text>
            )}
            <Typography.Text
              className={`network-message-content ${expanded ? "is-expanded" : ""}`}
              data-font-size={contentFontSize}
              type="secondary"
            >
              {networkMessageFullText(record)}
            </Typography.Text>
            {canExpand ? (
              <Button
                className="network-message-expand"
                type="link"
                size="small"
                onClick={() => toggleExpandedItem(record.id)}
              >
                {expanded ? "收起" : "展开全文"}
              </Button>
            ) : null}
          </div>
        );
      }
    }
  ];

  const data = overview.data;
  const zsxqSource = data?.sources.find((source) => source.source_type === "zsxq");

  const content = (
    <>
      <div className="page-header">
        <div>
          <Typography.Title level={2}>网络消息</Typography.Title>
          <Typography.Text type="secondary">按本地自选股筛选雪球发言和星球新闻，只采集展示，不触发推送。</Typography.Text>
        </div>
        <Space wrap>
          <StatusTag status={data?.source_status ?? data?.status} />
          <Button icon={<ReloadOutlined />} loading={overview.isFetching || forceRefresh.isPending} onClick={refresh}>
            刷新
          </Button>
          <Button icon={<PlayCircleOutlined />} loading={crawlHome.isPending} onClick={() => crawlHome.mutate()}>
            抓雪球发言
          </Button>
          <Button icon={<PlayCircleOutlined />} loading={crawlZsxq.isPending} onClick={() => crawlZsxq.mutate()}>
            抓星球新闻
          </Button>
          {!embedded ? (
            <Link to="/xueqiu/manage">
              <Button icon={<SettingOutlined />}>雪球设置</Button>
            </Link>
          ) : null}
        </Space>
      </div>

      {overview.isError ? <Alert type="error" showIcon message="网络消息接口暂时不可用" description={String(overview.error)} /> : null}

      <div className="panel">
        <div className="panel-toolbar">
          <Segmented
            options={["全部", "雪球发言", "星球新闻"]}
            value={filter}
            onChange={(value) => setFilter(String(value))}
          />
          <Space size={8} wrap>
            <Typography.Text type="secondary">正文字号</Typography.Text>
            <Segmented
              size="small"
              options={contentFontOptions}
              value={contentFontSize}
              onChange={(value) => setContentFontSize(String(value))}
            />
          </Space>
          <Typography.Text type="secondary">{data?.message}</Typography.Text>
        </div>
        <SourceStrip sources={data?.sources ?? []} />
      </div>

      {filter === "星球新闻" && zsxqSource ? (
        <Alert
          className="network-zsxq-alert"
          showIcon
          type="info"
          message="星球新闻接入状态"
          description={zsxqSource.message || "知识星球暂未配置。"}
        />
      ) : null}

      <div className="panel">
        <div className="panel-toolbar">
          <Typography.Text strong>相关自选股</Typography.Text>
          <Typography.Text type="secondary">
            {selectedStock === "all" ? `全部 ${filteredItems.length} 条` : `${parseStockLabel(selectedStock).name} ${visibleItems.length} 条`}
          </Typography.Text>
        </div>
        {stockGroups.length ? (
          <div className="network-stock-grid">
            <button
              className={`network-stock-card ${selectedStock === "all" ? "is-active" : ""}`}
              type="button"
              onClick={() => setSelectedStock("all")}
            >
              <span className="network-stock-name">全部自选股</span>
              <span className="network-stock-code">当前筛选</span>
              <span className="network-stock-count">{filteredItems.length} 条</span>
              <span className="network-stock-meta">
                星球 {allGroupCounts.zsxq} · 雪球 {allGroupCounts.xueqiu}
              </span>
            </button>
            {stockGroups.map((group) => (
              <button
                className={`network-stock-card ${selectedStock === group.label ? "is-active" : ""}`}
                type="button"
                key={group.label}
                onClick={() => setSelectedStock(group.label)}
              >
                <span className="network-stock-name">{group.name}</span>
                <span className="network-stock-code">{group.code || "自选股"}</span>
                <span className="network-stock-count">{group.count} 条</span>
                <span className="network-stock-meta">
                  星球 {group.zsxqCount} · 雪球 {group.xueqiuCount}
                </span>
              </button>
            ))}
          </div>
        ) : (
          <Empty description="暂无匹配自选股的网络消息" />
        )}
      </div>

      <div className="panel">
        <Table
          rowKey="id"
          loading={overview.isLoading}
          columns={columns}
          dataSource={visibleItems}
          locale={{ emptyText: <Empty description={filter === "星球新闻" ? "暂无匹配自选股的星球新闻" : "暂无网络消息"} /> }}
          pagination={{ pageSize: 12 }}
          rowClassName="network-message-row"
          scroll={{ x: 760 }}
        />
      </div>
    </>
  );

  if (embedded) {
    return <div className="network-messages-page network-messages-embedded">{content}</div>;
  }

  return <main className="page network-messages-page">{content}</main>;
}

export default function NetworkMessagesPage() {
  return <NetworkMessagesPanel />;
}
