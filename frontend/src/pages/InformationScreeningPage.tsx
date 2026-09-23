import {
  ArrowRightOutlined,
  CheckCircleOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  FilterOutlined,
  HistoryOutlined,
  InboxOutlined,
  LinkOutlined,
  ReloadOutlined,
  StarFilled
} from "@ant-design/icons";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Form, Input, Modal, Select, Space, Table, Tabs, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  informationScreeningApi,
  type InformationScreeningBatch,
  type InformationScreeningFeedbackItem,
  type InformationScreeningItem,
  type ScreeningPriceStatus,
  type ScreeningVerificationStatus
} from "../api/informationScreening";
import { industryTrendApi } from "../api/industryTrend";
import { decisionReviewApi, type DecisionCode, type DecisionCreateInput } from "../api/decisionReview";
import DecisionPolicyPanel, {
  DecisionPolicyInline,
  policyConsensusDefault
} from "../components/DecisionPolicyPanel";

type ScreeningTab = "top" | "verified" | "eye_catching" | "filtered";

interface DecisionFormValues {
  decision_code: DecisionCode;
  primary_stock_code?: string;
  primary_stock_name?: string;
  alternatives?: string;
  direction_verdict?: string;
  stock_verdict?: string;
  pricing_verdict: string;
  thesis: string;
  why_best?: string;
  trigger_conditions?: string;
  invalidation_conditions?: string;
  planned_horizon: 5 | 20 | 60;
}

const DELETE_REASON_RULES = [
  { value: "重复或旧逻辑", label: "重复或旧逻辑", rule: "同一线索不再生成；只有出现新的基本面变量才重新进入。" },
  { value: "缺少硬证据", label: "缺少硬证据", rule: "同主题下次必须新增公告、订单、客户或产业链交叉证据。" },
  { value: "产业映射太远", label: "产业映射太远", rule: "保留产业方向，但降低该股票映射权重。" },
  { value: "股价已充分交易", label: "股价已充分交易", rule: "只有新一轮基本面上修且股价尚未反映时才恢复。" },
  { value: "信息错误或已证伪", label: "信息错误或已证伪", rule: "阻止同一叙事自动回填，并将相同来源降权。" },
  { value: "时间窗口不符", label: "时间窗口不符", rule: "当前批次永久排除；只允许进入正确时间窗口。" },
  { value: "暂不关注该方向", label: "暂不关注该方向", rule: "降低同主题优先级，但遇到重大首次变化仍可重新提示。" },
  { value: "其他", label: "其他", rule: "保留快照与说明，供后续人工归类。" }
];

const VERIFICATION_LABELS: Record<ScreeningVerificationStatus, string> = {
  verified: "已验证",
  cross_verified: "交叉验证",
  partial: "部分验证",
  unverified: "暂未验证",
  disproved: "已证伪"
};

const PRICE_LABELS: Record<ScreeningPriceStatus, string> = {
  untraded: "尚未交易",
  first_expression: "首次表达",
  multi_rounds: "已交易多轮",
  negative: "负反馈",
  no_confirmation: "无盘面确认",
  unknown: "待判断"
};

function formatTime(value?: string | null) {
  if (!value) return "时间未注明";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function formatWindow(batch: InformationScreeningBatch | null) {
  if (!batch) return "等待下一次信息复盘";
  if (!batch.window_start && !batch.window_end) return "未限定时间窗口";
  return `${formatTime(batch.window_start)} — ${formatTime(batch.window_end)}`;
}

function verificationColor(value: ScreeningVerificationStatus) {
  if (value === "verified" || value === "cross_verified") return "green";
  if (value === "partial") return "gold";
  if (value === "disproved") return "red";
  return "default";
}

function priceColor(value: ScreeningPriceStatus) {
  if (value === "untraded" || value === "first_expression") return "cyan";
  if (value === "multi_rounds") return "volcano";
  if (value === "negative") return "red";
  return "default";
}

function officialCheckLabel(value: InformationScreeningItem["official_check_status"]) {
  if (value === "matched") return "官方公告命中";
  if (value === "not_found") return "官方已查未命中";
  if (value === "error") return "官方查询失败";
  if (value === "missing_stock") return "缺股票代码";
  return "无需公告核验";
}

function SourceLink({ item }: { item: InformationScreeningItem }) {
  const href = item.source_url || item.source_urls[0];
  if (!href) return <span>{item.source_name || item.source_type || "来源待补"}</span>;
  return <a href={href} target="_blank" rel="noreferrer"><LinkOutlined /> {item.source_name || item.source_type || "查看来源"}</a>;
}

function splitDecisionLines(value?: string) {
  return (value || "").split(/[\n；;,，]+/).map((part) => part.trim()).filter(Boolean);
}

function suggestedStock(item: InformationScreeningItem) {
  const raw = item.related_stocks[0] || "";
  const code = raw.match(/(?<!\d)(\d{6})(?!\d)/)?.[1] || item.official_stock_codes[0] || "";
  const name = raw.replace(/(?:SH|SZ|BJ)?\d{6}/gi, "").replace(/[()（）\[\]【】:\-—]/g, " ").trim();
  return { code, name: name || (code ? "" : raw) };
}

function suggestedDecisionCode(item: InformationScreeningItem): DecisionCode {
  return policyConsensusDefault(item.decision_policy);
}

function ScreeningCard({ item, onOpen, onDelete }: { item: InformationScreeningItem; onOpen: (item: InformationScreeningItem) => void; onDelete: (item: InformationScreeningItem) => void }) {
  const mapping = [...item.related_sectors, ...item.related_stocks][0];
  return (
    <article className={`screening-card bucket-${item.bucket}`}>
      <button
        type="button"
        className="screening-card-delete"
        aria-label={`删除 ${item.title}`}
        title="删除并记录原因"
        onClick={() => onDelete(item)}
      >
        <DeleteOutlined />
      </button>
      <button type="button" className="screening-card-open" onClick={() => onOpen(item)}>
        <div className="screening-card-compact-head">
          <Space size={6} wrap>
            {item.is_top ? <Tag color="gold" icon={<StarFilled />}>重点</Tag> : null}
            <Tag color={verificationColor(item.verification_status)}>{VERIFICATION_LABELS[item.verification_status]}</Tag>
          </Space>
          <span className="screening-importance">{item.importance}/5</span>
        </div>
        <Typography.Title level={4}>{item.title}</Typography.Title>
        <p className="screening-compact-change">{item.marginal_change || item.summary || "待补充核心变化"}</p>
        <DecisionPolicyInline evaluation={item.decision_policy} />
        <div className="screening-card-compact-footer">
          <span>{item.source_name || item.source_type || "来源待补"} · {formatTime(item.published_at)}</span>
          <span>{mapping ? `${mapping} · ` : ""}{PRICE_LABELS[item.price_status]} <ArrowRightOutlined /></span>
        </div>
      </button>
    </article>
  );
}

export default function InformationScreeningPage() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<ScreeningTab>("top");
  const [detailItem, setDetailItem] = useState<InformationScreeningItem | null>(null);
  const [promotingItem, setPromotingItem] = useState<InformationScreeningItem | null>(null);
  const [targetChainId, setTargetChainId] = useState<number | undefined>();
  const [deleteTarget, setDeleteTarget] = useState<InformationScreeningItem | null>(null);
  const [deleteReasonCategory, setDeleteReasonCategory] = useState<string>();
  const [deleteNote, setDeleteNote] = useState("");
  const [logOpen, setLogOpen] = useState(false);
  const [decisionItem, setDecisionItem] = useState<InformationScreeningItem | null>(null);
  const [decisionForm] = Form.useForm<DecisionFormValues>();

  const overviewQuery = useQuery({
    queryKey: ["information-screening", "overview"],
    queryFn: () => informationScreeningApi.overview()
  });
  const batches = overviewQuery.data?.batches ?? [];
  const batchDetailQueries = useQueries({
    queries: batches.map((batch) => ({
      queryKey: ["information-screening", "batch", batch.id],
      queryFn: () => informationScreeningApi.overview(batch.id),
      staleTime: 30_000
    }))
  });
  const chainsQuery = useQuery({
    queryKey: ["industry-trends", "promotion-options"],
    queryFn: () => industryTrendApi.list()
  });
  const feedbackQuery = useQuery({
    queryKey: ["information-screening-feedback"],
    queryFn: informationScreeningApi.feedback,
    enabled: logOpen
  });

  const promoteMutation = useMutation({
    mutationFn: ({ itemId, chainId }: { itemId: number; chainId: number }) => informationScreeningApi.promoteItem(itemId, chainId),
    onSuccess: (result) => {
      message.success(result.message);
      setPromotingItem(null);
      setTargetChainId(undefined);
      void queryClient.invalidateQueries({ queryKey: ["information-screening"] });
      void queryClient.invalidateQueries({ queryKey: ["industry-trends"] });
    },
    onError: (error: Error) => message.error(error.message || "转入失败")
  });
  const deleteMutation = useMutation({
    mutationFn: ({ itemId, reasonCategory, note }: { itemId: number; reasonCategory?: string; note?: string }) =>
      informationScreeningApi.deleteItem(itemId, { reason_category: reasonCategory, note }),
    onSuccess: async (result) => {
      setDeleteTarget(null);
      setDeleteReasonCategory(undefined);
      setDeleteNote("");
      await queryClient.invalidateQueries({ queryKey: ["information-screening"] });
      await queryClient.invalidateQueries({ queryKey: ["information-screening-feedback"] });
      message.success(result.message);
    },
    onError: (error: Error) => message.error(error.message || "删除失败")
  });
  const decisionMutation = useMutation({
    mutationFn: (payload: DecisionCreateInput) => decisionReviewApi.create(payload),
    onSuccess: async () => {
      setDecisionItem(null);
      decisionForm.resetFields();
      await queryClient.invalidateQueries({ queryKey: ["decision-review"] });
      message.success("已冻结为决策事件，后续只追加模拟表现");
    },
    onError: (error: Error) => {
      const matched = error.message.match(/"detail":"([^"]+)"/);
      message.error(matched?.[1] || error.message || "形成决策失败");
    }
  });

  const openDecision = (item: InformationScreeningItem) => {
    const stock = suggestedStock(item);
    decisionForm.setFieldsValue({
      decision_code: suggestedDecisionCode(item),
      primary_stock_code: stock.code,
      primary_stock_name: stock.name,
      alternatives: item.related_stocks.slice(1).join("；"),
      direction_verdict: item.evidence_summary || item.marginal_change,
      stock_verdict: item.related_stocks.length ? `当前映射：${item.related_stocks.join("、")}` : "待比较同链表达",
      pricing_verdict: item.price_summary || PRICE_LABELS[item.price_status],
      thesis: item.marginal_change || item.summary,
      why_best: "",
      trigger_conditions: item.validation_points.join("；"),
      invalidation_conditions: item.invalidation_conditions.join("；"),
      planned_horizon: 20
    });
    setDecisionItem(item);
    setDetailItem(null);
  };

  const submitDecision = (values: DecisionFormValues) => {
    if (!decisionItem) return;
    decisionMutation.mutate({
      information_item_id: decisionItem.id,
      decision_code: values.decision_code,
      primary_stock_code: values.primary_stock_code?.trim() || undefined,
      primary_stock_name: values.primary_stock_name?.trim() || undefined,
      alternatives: splitDecisionLines(values.alternatives),
      direction_verdict: values.direction_verdict?.trim(),
      stock_verdict: values.stock_verdict?.trim(),
      pricing_verdict: values.pricing_verdict.trim(),
      thesis: values.thesis.trim(),
      why_best: values.why_best?.trim(),
      trigger_conditions: splitDecisionLines(values.trigger_conditions),
      invalidation_conditions: splitDecisionLines(values.invalidation_conditions),
      planned_horizon: values.planned_horizon,
      cost_bps: 20
    });
  };

  const batchGroups = batches.map((batch, index) => ({
    batch: batchDetailQueries[index]?.data?.batch ?? batch,
    items: batchDetailQueries[index]?.data?.items
      ?? (overviewQuery.data?.batch?.id === batch.id ? overviewQuery.data.items : [])
  }));

  const filteredColumns: ColumnsType<InformationScreeningItem> = [
    { title: "时间", dataIndex: "published_at", width: 168, render: (value: string | null) => formatTime(value) },
    { title: "来源", dataIndex: "source_name", width: 130, render: (_value, item) => <SourceLink item={item} /> },
    { title: "已读信息", dataIndex: "title", ellipsis: true },
    { title: "涉及方向", width: 190, render: (_value, item) => [...item.related_sectors, ...item.related_stocks].join("、") || "-" },
    { title: "过滤原因", dataIndex: "filter_reason", width: 220 },
    { title: "恢复条件", dataIndex: "recovery_condition", width: 220, render: (value: string) => value || "出现新证据时重新评估" }
  ];

  const renderCards = (data: InformationScreeningItem[], emptyText: string) => data.length ? (
    <div className="screening-card-list">{data.map((item) => <ScreeningCard key={item.id} item={item} onOpen={setDetailItem} onDelete={(target) => { setDeleteTarget(target); setDeleteReasonCategory(undefined); setDeleteNote(""); }} />)}</div>
  ) : <div className="screening-empty screening-empty-compact"><Empty description={emptyText} /></div>;

  const stats = batches.reduce((total, batch) => ({
    read: total.read + batch.stats.read,
    deduplicated: total.deduplicated + batch.stats.deduplicated,
    top: total.top + batch.stats.top,
    verified: total.verified + batch.stats.verified,
    eye_catching: total.eye_catching + batch.stats.eye_catching,
    filtered: total.filtered + batch.stats.filtered,
    official_required: total.official_required + batch.stats.official_required,
    official_matched: total.official_matched + batch.stats.official_matched,
    official_unresolved: total.official_unresolved + batch.stats.official_unresolved
  }), { read: 0, deduplicated: 0, top: 0, verified: 0, eye_catching: 0, filtered: 0, official_required: 0, official_matched: 0, official_unresolved: 0 });

  const sourceScope = Array.from(new Set(batches.flatMap((item) => item.source_scope)));
  const startTimes = batches.map((item) => item.window_start).filter((value): value is string => Boolean(value)).sort();
  const endTimes = batches.map((item) => item.window_end).filter((value): value is string => Boolean(value)).sort();
  const overallWindow = batches.length
    ? `${formatTime(startTimes[0])} — ${formatTime(endTimes[endTimes.length - 1])}`
    : "等待下一次信息复盘";

  const itemsForTab = (items: InformationScreeningItem[], tab: ScreeningTab) => {
    if (tab === "top") return items.filter((item) => item.is_top && item.bucket !== "filtered");
    if (tab === "verified") return items.filter((item) => item.bucket === "verified");
    if (tab === "eye_catching") return items.filter((item) => item.bucket === "eye_catching");
    return items.filter((item) => item.bucket === "filtered");
  };

  const renderBatchGroups = (tab: ScreeningTab) => batchGroups.length ? (
    <div className="screening-batch-groups">
      {batchGroups.map(({ batch, items }) => {
        const groupItems = itemsForTab(items, tab);
        return (
          <section className="screening-batch-group" id={`screening-batch-${batch.id}`} key={batch.id}>
            <header className="screening-batch-group-head">
              <div>
                <strong>{batch.title}</strong>
                <span>{formatWindow(batch)}</span>
              </div>
              <div>
                <Tag>已读 {batch.stats.read}</Tag>
                <Tag color="green">验证 {batch.stats.verified}</Tag>
                <Tag color="gold">待证 {batch.stats.eye_catching}</Tag>
              </div>
            </header>
            {tab === "filtered"
              ? (groupItems.length
                ? <Table rowKey="id" columns={filteredColumns} dataSource={groupItems} pagination={false} scroll={{ x: 1120 }} />
                : <div className="screening-empty screening-empty-compact"><Empty description="本窗口没有过滤记录" /></div>)
              : renderCards(groupItems, `本窗口暂无${tab === "top" ? "重点结果" : tab === "verified" ? "已验证变化" : "抢眼待证"}`)}
          </section>
        );
      })}
    </div>
  ) : <div className="screening-empty"><Empty description="尚无信息筛选批次" /></div>;

  const tabItems = [
    { key: "top", label: `重点结果 ${stats.top}`, children: renderBatchGroups("top") },
    { key: "verified", label: `已验证变化 ${stats.verified}`, children: renderBatchGroups("verified") },
    { key: "eye_catching", label: `抢眼待证 ${stats.eye_catching}`, children: renderBatchGroups("eye_catching") },
    { key: "filtered", label: `已读过滤 ${stats.filtered}`, children: renderBatchGroups("filtered") }
  ];

  return (
    <main className="information-screening-page">
      <header className="screening-page-header">
        <div>
          <Typography.Title level={2}>信息筛选</Typography.Title>
          <Typography.Text>边际变化通过验证后，再进入产业研究。</Typography.Text>
        </div>
        <Space wrap>
          <Button icon={<HistoryOutlined />} onClick={() => setLogOpen(true)}>筛选日志</Button>
          <Button
            icon={<ReloadOutlined />}
            loading={overviewQuery.isFetching || batchDetailQueries.some((query) => query.isFetching)}
            onClick={() => void queryClient.invalidateQueries({ queryKey: ["information-screening"] })}
          >刷新</Button>
        </Space>
      </header>

      <section className="screening-window-bar">
        <div className="screening-window-summary">
          <div className="screening-window-summary-head">
            <span>复盘时间轴</span>
            <strong>{batches.length ? `${batches.length} 个窗口` : "尚无复盘批次"}</strong>
          </div>
          <small>{overallWindow}</small>
          <details className="screening-source-details">
            <summary>
              数据源 {sourceScope.length}
              {stats.official_required ? ` · 官方核验 ${stats.official_matched}/${stats.official_required}` : ""}
            </summary>
            <div className="screening-source-scope">
              {sourceScope.length ? sourceScope.map((source) => <Tag key={source}>{source}</Tag>) : <Tag>等待对话写入</Tag>}
              {stats.official_required ? <Tag color={stats.official_unresolved ? "orange" : "green"}>官方核验 {stats.official_matched}/{stats.official_required}</Tag> : null}
            </div>
          </details>
        </div>
        <div className="screening-window-list" aria-label="复盘时间窗口">
          {batches.map((item) => (
            <button
              type="button"
              key={item.id}
              onClick={() => document.getElementById(`screening-batch-${item.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" })}
            >
              <strong>{item.title}</strong>
              <span>重点 {item.stats.top} · 验证 {item.stats.verified} · 待证 {item.stats.eye_catching}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="screening-flow" aria-label="信息处理统计">
        <button type="button"><InboxOutlined /><span>已读取</span><strong>{stats.read}</strong></button>
        <ArrowRightOutlined />
        <button type="button"><FilterOutlined /><span>去重后</span><strong>{stats.deduplicated}</strong></button>
        <ArrowRightOutlined />
        <button type="button" onClick={() => setActiveTab("top")}><StarFilled /><span>重点结果</span><strong>{stats.top}</strong></button>
      </section>

      <section className="screening-results-panel">
        <Tabs activeKey={activeTab} onChange={(key) => setActiveTab(key as ScreeningTab)} items={tabItems} />
      </section>

      <Modal
        className="screening-detail-modal"
        width={860}
        title="信息详情"
        open={Boolean(detailItem)}
        onCancel={() => setDetailItem(null)}
        footer={[
          <Button key="close" onClick={() => setDetailItem(null)}>关闭</Button>,
          <Button
            key="decision"
            type="primary"
            icon={<ExperimentOutlined />}
            onClick={() => { if (detailItem) openDecision(detailItem); }}
          >
            形成决策
          </Button>,
          detailItem?.promoted_chain_id ? (
            <Link key="promoted" className="screening-detail-promoted" to="/investment/industry-trend" onClick={() => setDetailItem(null)}>
              <CheckCircleOutlined /> 查看产业趋势
            </Link>
          ) : (
            <Button
              key="promote"
              icon={<ArrowRightOutlined />}
              onClick={() => { if (detailItem) setPromotingItem(detailItem); setDetailItem(null); }}
            >
              转入产业趋势
            </Button>
          )
        ]}
      >
        {detailItem ? (
          <div className="screening-detail-content">
            <Space size={6} wrap>
              {detailItem.is_top ? <Tag color="gold" icon={<StarFilled />}>重点</Tag> : null}
              <Tag color={verificationColor(detailItem.verification_status)}>{VERIFICATION_LABELS[detailItem.verification_status]}</Tag>
              <Tag color={priceColor(detailItem.price_status)}>{PRICE_LABELS[detailItem.price_status]}</Tag>
              {detailItem.official_check_status !== "not_required" ? <Tag color={detailItem.official_check_status === "matched" ? "green" : "orange"}>{officialCheckLabel(detailItem.official_check_status)}</Tag> : null}
              <span className="screening-importance">重要度 {detailItem.importance}/5</span>
            </Space>
            <Typography.Title level={3}>{detailItem.title}</Typography.Title>
            <div className="screening-source-line"><SourceLink item={detailItem} /><span>{formatTime(detailItem.published_at)}</span></div>
            <DecisionPolicyPanel evaluation={detailItem.decision_policy} />
            {detailItem.summary ? <p className="screening-detail-summary">{detailItem.summary}</p> : null}
            {detailItem.official_check_status !== "not_required" ? (
              <div className="screening-official-check">
                <strong>{officialCheckLabel(detailItem.official_check_status)}</strong>
                <span>{detailItem.official_check_message}</span>
                {detailItem.official_source_url ? <a href={detailItem.official_source_url} target="_blank" rel="noreferrer"><LinkOutlined /> 打开官方公告</a> : null}
              </div>
            ) : null}
            <div className="screening-detail-grid">
              <section><span>边际变化 / 抢眼点</span><p>{detailItem.marginal_change || "待补充"}</p></section>
              <section><span>验证证据</span><p>{detailItem.evidence_summary || "尚无独立证据，保留为线索"}</p></section>
              <section><span>股价已反映什么</span><p>{detailItem.price_summary || PRICE_LABELS[detailItem.price_status]}</p></section>
              <section>
                <span>下一步验证</span>
                {detailItem.validation_points.length ? <ul>{detailItem.validation_points.map((point) => <li key={point}>{point}</li>)}</ul> : <p>待设置验证动作</p>}
              </section>
            </div>
            {(detailItem.related_sectors.length || detailItem.related_stocks.length) ? (
              <div className="screening-mappings">
                {detailItem.related_sectors.map((sector) => <Tag key={`sector-${sector}`}>{sector}</Tag>)}
                {detailItem.related_stocks.map((stock) => <Tag color="blue" key={`stock-${stock}`}>{stock}</Tag>)}
              </div>
            ) : null}
            {detailItem.invalidation_conditions.length ? (
              <div className="screening-invalidation"><strong>证伪条件：</strong>{detailItem.invalidation_conditions.join("；")}</div>
            ) : null}
          </div>
        ) : null}
      </Modal>

      <Modal
        width={880}
        title={decisionItem ? `形成冻结决策｜${decisionItem.title}` : "形成冻结决策"}
        open={Boolean(decisionItem)}
        okText="冻结并开始模拟"
        cancelText="取消"
        confirmLoading={decisionMutation.isPending}
        onCancel={() => { if (!decisionMutation.isPending) { setDecisionItem(null); decisionForm.resetFields(); } }}
        onOk={() => decisionForm.submit()}
      >
        <Alert
          showIcon
          type="info"
          message="人工判断会单独冻结，不会被qq或简单基线覆盖"
          description="两套策略只是对照建议。若两者有分歧，默认先放在C类，你可以根据当时证据人工改判；所有等级都保留后续表现。"
        />
        <DecisionPolicyPanel evaluation={decisionItem?.decision_policy} />
        <Form<DecisionFormValues>
          className="decision-create-form"
          form={decisionForm}
          layout="vertical"
          onFinish={submitDecision}
        >
          <div className="decision-create-grid decision-create-grid-3">
            <Form.Item name="decision_code" label="人工冻结判断" rules={[{ required: true }]}>
              <Select options={[
                { value: "A", label: "A｜进入模拟买入池" },
                { value: "B", label: "B｜等待价格或催化" },
                { value: "C", label: "C｜等待验证触发器" },
                { value: "D", label: "D｜过滤但保留样本" }
              ]} />
            </Form.Item>
            <Form.Item
              name="primary_stock_code"
              label="主选股票代码"
              dependencies={["decision_code"]}
              rules={[({ getFieldValue }) => ({
                validator: (_rule, value) => getFieldValue("decision_code") === "D" || String(value || "").trim()
                  ? Promise.resolve()
                  : Promise.reject(new Error("A/B/C 必须填写股票代码"))
              })]}
            >
              <Input placeholder="例如 688800" maxLength={16} />
            </Form.Item>
            <Form.Item
              name="primary_stock_name"
              label="主选股票名称"
              dependencies={["decision_code"]}
              rules={[({ getFieldValue }) => ({
                validator: (_rule, value) => getFieldValue("decision_code") === "D" || String(value || "").trim()
                  ? Promise.resolve()
                  : Promise.reject(new Error("A/B/C 必须填写股票名称"))
              })]}
            >
              <Input placeholder="例如 瑞可达" maxLength={80} />
            </Form.Item>
          </div>
          <div className="decision-create-grid">
            <Form.Item name="thesis" label="核心基本面变化" rules={[{ required: true, message: "请写清这次决策依据的变化" }]}>
              <Input.TextArea autoSize={{ minRows: 3, maxRows: 6 }} />
            </Form.Item>
            <Form.Item name="pricing_verdict" label="股价已反映 / 尚未反映" rules={[{ required: true, message: "必须判断当前定价状态" }]}>
              <Input.TextArea autoSize={{ minRows: 3, maxRows: 6 }} />
            </Form.Item>
          </div>
          <div className="decision-create-grid">
            <Form.Item name="direction_verdict" label="方向是否成立"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
            <Form.Item name="stock_verdict" label="标的是否最直接"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
          </div>
          <div className="decision-create-grid">
            <Form.Item name="why_best" label="为什么主选优于同链"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} placeholder="比较客户、弹性、兑现速度和市场表达" /></Form.Item>
            <Form.Item name="alternatives" label="备选股票"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} placeholder="用逗号或分号分隔" /></Form.Item>
          </div>
          <div className="decision-create-grid">
            <Form.Item name="trigger_conditions" label="升级 / 买入触发条件"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
            <Form.Item name="invalidation_conditions" label="证伪条件"><Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
          </div>
          <Form.Item name="planned_horizon" label="计划观察周期" rules={[{ required: true }]}>
            <Select style={{ width: 220 }} options={[
              { value: 5, label: "5个交易日｜事件催化" },
              { value: 20, label: "20个交易日｜基本面变化" },
              { value: 60, label: "60个交易日｜产业趋势" }
            ]} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={Boolean(deleteTarget)}
        title={deleteTarget ? `删除「${deleteTarget.title}」` : "删除卡片"}
        okText="删除并形成反馈"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        confirmLoading={deleteMutation.isPending}
        onCancel={() => { if (!deleteMutation.isPending) setDeleteTarget(null); }}
        onOk={() => deleteTarget && deleteMutation.mutate({
          itemId: deleteTarget.id,
          reasonCategory: deleteReasonCategory,
          note: deleteNote.trim() || undefined
        })}
      >
        <div className="screening-delete-form">
          <Typography.Text type="secondary">删除后保留完整卡片快照；同一线索不会被下一批自动重新生成。</Typography.Text>
          <label>
            <span>删除原因</span>
            <Select
              allowClear
              value={deleteReasonCategory}
              placeholder="选择原因，训练下一轮筛选"
              options={DELETE_REASON_RULES.map(({ value, label }) => ({ value, label }))}
              onChange={setDeleteReasonCategory}
            />
          </label>
          {deleteReasonCategory ? (
            <div className="screening-feedback-rule">
              <strong>下次筛选规则</strong>
              <span>{DELETE_REASON_RULES.find((item) => item.value === deleteReasonCategory)?.rule}</span>
            </div>
          ) : null}
          <label>
            <span>补充说明（可选）</span>
            <Input.TextArea
              value={deleteNote}
              maxLength={500}
              showCount
              autoSize={{ minRows: 3, maxRows: 5 }}
              placeholder="例如：只有概念映射，没有客户或订单证据"
              onChange={(event) => setDeleteNote(event.target.value)}
            />
          </label>
        </div>
      </Modal>

      <Modal
        open={logOpen}
        width={1080}
        footer={null}
        destroyOnClose
        title={<Space><HistoryOutlined /><span>信息筛选日志</span></Space>}
        onCancel={() => setLogOpen(false)}
      >
        <Tabs
          items={[
            {
              key: "batches",
              label: `生成记录 ${batches.length}`,
              children: (
                <Table<InformationScreeningBatch>
                  rowKey="id"
                  dataSource={batches}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  columns={[
                    { title: "生成时间", dataIndex: "created_at", width: 170, render: formatTime },
                    { title: "复盘窗口", dataIndex: "title", render: (value: string, row) => <div className="screening-log-title"><strong>{value}</strong><span>{formatWindow(row)}</span></div> },
                    { title: "已读 / 去重", width: 120, render: (_value, row) => `${row.stats.read} / ${row.stats.deduplicated}` },
                    { title: "验证 / 待证 / 过滤", width: 170, render: (_value, row) => `${row.stats.verified} / ${row.stats.eye_catching} / ${row.stats.filtered}` }
                  ]}
                />
              )
            },
            {
              key: "feedback",
              label: `删除反馈 ${feedbackQuery.data?.items.length ?? 0}`,
              children: (
                <Table<InformationScreeningFeedbackItem>
                  rowKey="id"
                  loading={feedbackQuery.isLoading}
                  dataSource={feedbackQuery.data?.items ?? []}
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  scroll={{ x: 920 }}
                  locale={{ emptyText: <Empty description="暂无删除反馈" /> }}
                  columns={[
                    { title: "删除时间", dataIndex: "created_at", width: 170, render: formatTime },
                    { title: "线索", dataIndex: "title", width: 300, render: (value: string, row) => <div className="screening-log-title"><strong>{value}</strong><span>{row.source_name || row.source_type}</span></div> },
                    { title: "原状态", dataIndex: "verification_status", width: 105, render: (value: ScreeningVerificationStatus) => <Tag color={verificationColor(value)}>{VERIFICATION_LABELS[value]}</Tag> },
                    { title: "删除原因", dataIndex: "reason_category", width: 145, render: (value: string | null) => value ? <Tag color="orange">{value}</Tag> : <Tag>未分类</Tag> },
                    { title: "补充说明", dataIndex: "note", render: (value: string | null) => value || <Typography.Text type="secondary">未填写</Typography.Text> }
                  ]}
                />
              )
            }
          ]}
        />
      </Modal>

      <Modal
        title="转入产业趋势"
        open={Boolean(promotingItem)}
        okText="确认转入"
        cancelText="取消"
        okButtonProps={{ disabled: !targetChainId, loading: promoteMutation.isPending }}
        onCancel={() => { setPromotingItem(null); setTargetChainId(undefined); }}
        onOk={() => promotingItem && targetChainId && promoteMutation.mutate({ itemId: promotingItem.id, chainId: targetChainId })}
      >
        <Typography.Paragraph>{promotingItem?.title}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">
          已验证变化会写入产业证据和跟踪时间轴；待验证线索会进入验证路线图。
        </Typography.Paragraph>
        <Select
          aria-label="选择产业趋势"
          showSearch
          optionFilterProp="label"
          placeholder="选择要接收这条信息的产业"
          style={{ width: "100%" }}
          value={targetChainId}
          loading={chainsQuery.isLoading}
          options={(chainsQuery.data?.items ?? []).map((chain) => ({ value: chain.id, label: chain.name }))}
          onChange={setTargetChainId}
        />
      </Modal>
    </main>
  );
}
