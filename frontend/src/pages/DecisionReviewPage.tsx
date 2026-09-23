import {
  ArrowRightOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Progress, Space, Table, Tabs, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  decisionReviewApi,
  type BacktestMetric,
  type DecisionCode,
  type DecisionEvent,
  type FiveYearEventRow,
  type FiveYearMarketResult
} from "../api/decisionReview";
import { DecisionPolicyInline } from "../components/DecisionPolicyPanel";

const DECISION_META: Record<DecisionCode, { label: string; color: string; description: string }> = {
  A: { label: "A 模拟买入", color: "green", description: "变化成立、表达正确、尚未充分定价" },
  B: { label: "B 等价格/催化", color: "gold", description: "逻辑成立，但赔率或时点暂时不合适" },
  C: { label: "C 等验证", color: "blue", description: "线索重要，但仍缺升级触发器" },
  D: { label: "D 过滤", color: "default", description: "旧逻辑、映射错误或证据不成立" }
};

const STATUS_LABELS: Record<string, string> = {
  waiting_entry: "等待下一交易日",
  tracking: "模拟跟踪中",
  matured: "计划周期已完成",
  shadow_waiting: "影子样本待成交",
  shadow_tracking: "影子样本跟踪中",
  shadow_matured: "影子样本已成熟",
  no_stock: "仅记录方向"
};

function formatTime(value?: string | null) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", { hour12: false });
}

function formatPct(value?: number | null) {
  if (value === null || value === undefined) return "待形成";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function metricTone(value?: number | null) {
  if (value === null || value === undefined) return "is-pending";
  return value >= 0 ? "is-positive" : "is-negative";
}

function HorizonCell({ row, horizon }: { row: DecisionEvent; horizon: number }) {
  const result = row.performance.horizons?.[String(horizon)];
  if (!result?.available) return <span className="decision-pending">待 {horizon} 个交易日</span>;
  return (
    <div className="decision-return-cell">
      <strong className={metricTone(result.return_pct)}>{formatPct(result.return_pct)}</strong>
      <small>MFE {formatPct(result.mfe_pct)} · MAE {formatPct(result.mae_pct)}</small>
    </div>
  );
}

function MetricPanel({ title, metric }: { title: string; metric: BacktestMetric }) {
  return (
    <article className="decision-metric-panel">
      <header><strong>{title}</strong><Tag>{metric.sample_count} 个成熟样本</Tag></header>
      <div>
        <span>期望收益<strong className={metricTone(metric.expected_value_pct)}>{formatPct(metric.expected_value_pct)}</strong></span>
        <span>胜率<strong>{metric.win_rate === null ? "待形成" : `${metric.win_rate.toFixed(1)}%`}</strong></span>
        <span>盈亏比<strong>{metric.payoff_ratio === null ? "待形成" : metric.payoff_ratio.toFixed(2)}</strong></span>
        <span>沪深300超额<strong className={metricTone(metric.avg_excess_return_pct)}>{formatPct(metric.avg_excess_return_pct)}</strong></span>
      </div>
      <footer>平均 MFE {formatPct(metric.avg_mfe_pct)} · 平均 MAE {formatPct(metric.avg_mae_pct)}</footer>
    </article>
  );
}

function validationStateMeta(state?: string) {
  if (state === "validated") return { color: "green", label: "严格通过" };
  if (state === "useful_with_limits") return { color: "gold", label: "有限可用" };
  return { color: "red", label: "未验证" };
}

function numeric(value?: string | number | null) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function FiveYearMarketPanel({ market, result }: { market: "A股" | "美股"; result: FiveYearMarketResult }) {
  const state = validationStateMeta(result.verdict.state);
  const rows = (["A", "B", "C", "D"] as DecisionCode[]).map((code) => ({
    code,
    ...result.validation.by_decision[code]
  }));
  return (
    <article className="five-year-market-panel">
      <header>
        <div><strong>{market}</strong><span>2024年以后留出样本，只验不改阈值</span></div>
        <Tag color={state.color}>{state.label}</Tag>
      </header>
      <p>{result.verdict.interpretation}</p>
      <div className="five-year-gain-grid">
        <div><span>验证期事件</span><strong>{result.event_counts.validation ?? 0}</strong></div>
        <div><span>成熟20日样本</span><strong>{result.event_counts.mature_20d ?? 0}</strong></div>
        <div><span>A对B定价增益</span><strong className={metricTone(result.validation.pricing_gain_a_vs_b_median_pct)}>{formatPct(result.validation.pricing_gain_a_vs_b_median_pct)}</strong></div>
        <div><span>A对B/C筛选增益</span><strong className={metricTone(result.validation.filter_gain_a_vs_bc_median_pct)}>{formatPct(result.validation.filter_gain_a_vs_bc_median_pct)}</strong></div>
      </div>
      <Table
        size="small"
        rowKey="code"
        pagination={false}
        dataSource={rows}
        columns={[
          { title: "分层", dataIndex: "code", render: (code: DecisionCode) => <Tag color={DECISION_META[code].color}>{code}</Tag> },
          { title: "样本", dataIndex: "sample_count" },
          { title: "胜率", dataIndex: "win_rate_pct", render: (value: number | null) => value == null ? "-" : `${value.toFixed(1)}%` },
          { title: "中位超额", dataIndex: "median_excess_return_pct", render: (value: number | null) => <span className={metricTone(value)}>{formatPct(value)}</span> },
          { title: "平均超额", dataIndex: "avg_excess_return_pct", render: (value: number | null) => <span className={metricTone(value)}>{formatPct(value)}</span> },
          { title: "95%区间", render: (_value, row) => row.bootstrap_95ci_low_pct == null ? "-" : `${formatPct(row.bootstrap_95ci_low_pct)} ～ ${formatPct(row.bootstrap_95ci_high_pct)}` }
        ]}
      />
      <details className="five-year-thresholds">
        <summary>查看训练期冻结阈值</summary>
        <div>
          <span>证据增长 ≥ {result.thresholds.evidence_growth_pct}%</span>
          <span>加速度 ≥ {result.thresholds.evidence_acceleration_pct}%</span>
          <span>未定价20日超额 ≤ {result.thresholds.unpriced_20d_excess_pct}%</span>
          <span>已定价20日超额 ≥ {result.thresholds.priced_20d_excess_pct}%</span>
          <span>确认日相对基准 ≥ {result.thresholds.confirmation_excess_pct}%</span>
        </div>
      </details>
    </article>
  );
}

function FiveYearValidationPanel() {
  const queryClient = useQueryClient();
  const overviewQuery = useQuery({
    queryKey: ["decision-review-five-year"],
    queryFn: decisionReviewApi.fiveYearValidation,
    refetchInterval: (query) => query.state.data?.status.status === "running" ? 3000 : false
  });
  const eventsQuery = useQuery({
    queryKey: ["decision-review-five-year-events"],
    queryFn: decisionReviewApi.fiveYearEvents,
    enabled: Boolean(overviewQuery.data?.summary)
  });
  const refreshMutation = useMutation({
    mutationFn: decisionReviewApi.refreshFiveYearValidation,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["decision-review-five-year"] });
      message.success("五年验证已在后台更新；页面会自动显示进度");
    },
    onError: (error: Error) => message.error(error.message || "启动失败")
  });
  const data = overviewQuery.data;
  const summary = data?.summary;
  const run = data?.status;
  const overall = validationStateMeta(summary?.overall_verdict.state);
  const eventColumns: ColumnsType<FiveYearEventRow> = [
    { title: "市场", dataIndex: "market", width: 70, render: (value) => <Tag>{value}</Tag> },
    { title: "披露日", dataIndex: "signal_date", width: 110 },
    { title: "公司", width: 150, render: (_value, row) => <div className="decision-stock-cell"><strong>{row.name}</strong><span>{row.symbol}</span></div> },
    { title: "分层", dataIndex: "decision_code", width: 70, render: (value: DecisionCode) => <Tag color={DECISION_META[value].color}>{value}</Tag> },
    { title: "证据/定价", width: 180, render: (_value, row) => <span>{row.evidence_state} · {row.pricing_state}</span> },
    { title: "利润变化", dataIndex: "profit_growth_pct", width: 105, render: (value) => formatPct(numeric(value)) },
    { title: "披露前20日超额", dataIndex: "pre_20d_excess_pct", width: 140, render: (value) => formatPct(numeric(value)) },
    { title: "确认日超额", dataIndex: "market_confirmation_excess_pct", width: 115, render: (value) => <span className={metricTone(numeric(value))}>{formatPct(numeric(value))}</span> },
    { title: "20日市场超额", width: 130, render: (_value, row) => <span className={metricTone(numeric(row.market_excess_return_pct_20d))}>{formatPct(numeric(row.market_excess_return_pct_20d))}</span> },
    { title: "观察/模拟成交", width: 190, render: (_value, row) => <span>{row.observation_date || "无观察日"} → {row.entry_date || "不可成交"}{numeric(row.observation_deferred_sessions) ? ` · 观察顺延${row.observation_deferred_sessions}日` : ""}</span> },
    { title: "原始证据", width: 100, render: (_value, row) => row.source_url ? <a href={row.source_url} target="_blank" rel="noreferrer">打开</a> : "-" }
  ];
  return (
    <section className="five-year-validation-panel">
      <header className="five-year-validation-head">
        <div>
          <strong>预期差五年验证</strong>
          <span>先冻结当时可得证据，再判断是否已定价；A股与美股分别训练、共同接受留出检验</span>
        </div>
        <Button loading={refreshMutation.isPending || run?.status === "running"} onClick={() => refreshMutation.mutate()}>重新跑五年验证</Button>
      </header>
      {run?.status === "running" ? (
        <div className="five-year-run-progress">
          <Progress percent={run.progress_pct ?? 0} status="active" />
          <span>{run.message || "正在更新"}</span>
        </div>
      ) : null}
      {!summary ? (
        <Empty description={run?.message || "还没有五年验证结果"} />
      ) : (
        <>
          <Alert
            showIcon
            type={summary.overall_verdict.state === "validated" ? "success" : summary.overall_verdict.state === "useful_with_limits" ? "warning" : "error"}
            message={<Space><Tag color={overall.color}>{overall.label}</Tag><span>{summary.period.start} 至 {summary.period.end} · {summary.period.years.toFixed(2)}年</span></Space>}
            description={summary.overall_verdict.interpretation}
          />
          <div className="five-year-market-grid">
            <FiveYearMarketPanel market="A股" result={summary.markets.A股} />
            <FiveYearMarketPanel market="美股" result={summary.markets.美股} />
          </div>
          <div className="five-year-method-card">
            <strong>防前视与市场差异</strong>
            <div>
              {Object.entries(data?.source_manifest?.rules ?? {}).map(([key, value]) => <p key={key}><b>{key}</b><span>{value}</span></p>)}
            </div>
            {(data?.source_manifest?.limitations ?? []).length ? <details><summary>覆盖限制与仍待补全</summary>{data!.source_manifest!.limitations!.map((item) => <p key={item}>{item}</p>)}</details> : null}
          </div>
          <Table
            className="five-year-event-table"
            rowKey="event_key"
            size="small"
            loading={eventsQuery.isLoading}
            dataSource={eventsQuery.data?.items ?? []}
            columns={eventColumns}
            scroll={{ x: 1370 }}
            pagination={{ pageSize: 20, showSizeChanger: false }}
          />
        </>
      )}
    </section>
  );
}

export default function DecisionReviewPage() {
  const queryClient = useQueryClient();
  const overviewQuery = useQuery({
    queryKey: ["decision-review"],
    queryFn: decisionReviewApi.overview
  });
  const refreshMutation = useMutation({
    mutationFn: decisionReviewApi.refresh,
    onSuccess: (data) => {
      queryClient.setQueryData(["decision-review"], data);
      message.success("模拟行情与回测结果已更新");
    },
    onError: (error: Error) => message.error(error.message || "更新失败")
  });
  const data = overviewQuery.data;
  const rows = data?.items ?? [];
  const summary = data?.summary;

  const columns: ColumnsType<DecisionEvent> = [
    {
      title: "冻结决策",
      width: 310,
      render: (_value, row) => (
        <div className="decision-title-cell">
          <Space size={6} wrap>
            <Tag color={DECISION_META[row.decision_code].color}>{DECISION_META[row.decision_code].label}</Tag>
            <Tag color={row.source_kind === "industry_trend" ? "cyan" : "default"}>{row.source_kind === "industry_trend" ? "产业决策" : "信息决策"}</Tag>
            <span>{formatTime(row.signal_at)}</span>
          </Space>
          <strong>{row.information_title}</strong>
          <small>{row.source_name || row.source_type || "来源待补"}</small>
        </div>
      )
    },
    {
      title: "策略对照",
      width: 255,
      render: (_value, row) => (
        <div className="decision-policy-table-cell">
          <DecisionPolicyInline evaluation={row.policy_evaluation} />
          <small>
            人工 {row.decision_code}
            {row.policy_evaluation?.manual_aligned_with_all ? " · 与两套策略一致" : " · 与至少一套策略不同"}
          </small>
          {!row.policy_evaluation?.quality_complete ? <span>基础数据仍有缺口</span> : null}
        </div>
      )
    },
    {
      title: "主选表达",
      width: 160,
      render: (_value, row) => (
        <div className="decision-stock-cell">
          <strong>{row.primary_stock_name || "仅记录方向"}</strong>
          <span>{row.primary_full_code || "-"}</span>
          <small>{row.entry_price ? `模拟开盘价 ${row.entry_price.toFixed(2)}` : STATUS_LABELS[row.execution_status]}</small>
        </div>
      )
    },
    {
      title: "当时判断",
      width: 330,
      render: (_value, row) => (
        <div className="decision-thesis-cell">
          <strong>{row.thesis}</strong>
          <span>定价：{row.pricing_verdict}</span>
          {row.trigger_conditions.length ? <small>升级触发：{row.trigger_conditions.join("；")}</small> : null}
        </div>
      )
    },
    { title: "5日", width: 150, render: (_value, row) => <HorizonCell row={row} horizon={5} /> },
    { title: "20日", width: 150, render: (_value, row) => <HorizonCell row={row} horizon={20} /> },
    { title: "60日", width: 150, render: (_value, row) => <HorizonCell row={row} horizon={60} /> },
    {
      title: "状态",
      width: 145,
      render: (_value, row) => <Tag color={row.execution_status.includes("matured") ? "green" : "processing"}>{STATUS_LABELS[row.execution_status] || row.execution_status}</Tag>
    }
  ];

  const queueTable = (tableRows: DecisionEvent[]) => (
    <Table
      rowKey="decision_key"
      columns={columns}
      dataSource={tableRows}
      loading={overviewQuery.isLoading}
      pagination={{ pageSize: 15, showSizeChanger: false }}
      scroll={{ x: 1650 }}
      locale={{ emptyText: <Empty description="还没有冻结决策；可从产业趋势或信息卡片点击“形成决策”开始" /> }}
    />
  );

  return (
    <main className="decision-review-page">
      <header className="decision-review-header">
        <div>
          <Typography.Title level={2}>决策复盘</Typography.Title>
          <Typography.Text type="secondary">检验判断是否真正提高收益质量。</Typography.Text>
        </div>
        <Button icon={<ReloadOutlined />} loading={overviewQuery.isFetching || refreshMutation.isPending} onClick={() => refreshMutation.mutate()}>
          更新模拟行情
        </Button>
      </header>

      <Alert
        showIcon
        type="info"
        message="人工、qq实验策略与简单基线分开复盘；策略不覆盖人工判断，也不会产生真实委托。"
      />

      <section className="decision-kpis">
        <article><SafetyCertificateOutlined /><span>策略有分歧</span><strong>{summary?.policy_disagreement_total ?? 0}</strong></article>
        <article><ClockCircleOutlined /><span>人工与策略不同</span><strong>{summary?.manual_disagreement_total ?? 0}</strong></article>
        <article><CheckCircleOutlined /><span>成熟样本</span><strong>{summary?.matured ?? 0}</strong></article>
      </section>

      <section className="decision-pipeline" aria-label="筛选到决策的样本漏斗">
        <div><span>全部信息</span><strong>{summary?.pipeline.information_total ?? 0}</strong></div>
        <ArrowRightOutlined />
        <div><span>硬证据已验证</span><strong>{summary?.pipeline.verified_total ?? 0}</strong></div>
        <ArrowRightOutlined />
        <div><span>形成冻结决策</span><strong>{summary?.pipeline.decision_total ?? 0}</strong><small>其中产业决策 {summary?.pipeline.industry_decision_total ?? 0}</small></div>
        <ArrowRightOutlined />
        <div><span>A类模拟成交</span><strong>{summary?.pipeline.simulated_total ?? 0}</strong></div>
      </section>

      <section className="decision-review-panel">
        <Tabs
          items={[
            { key: "queue", label: `决策队列 ${rows.length}`, children: queueTable(rows) },
            { key: "disagreement", label: `策略分歧 ${summary?.policy_disagreement_total ?? 0}`, children: queueTable(rows.filter((row) => row.policy_evaluation?.disagreement)) },
            { key: "simulated", label: `A类模拟 ${summary?.decision_counts.A ?? 0}`, children: queueTable(rows.filter((row) => row.decision_code === "A")) },
            {
              key: "metrics",
              label: "回测指标",
              children: (
                <div className="decision-metrics-content">
                  <div className="decision-horizon-grid">
                    {[5, 20, 60].map((horizon) => <MetricPanel key={horizon} title={`${horizon}日全样本`} metric={summary?.horizons[String(horizon)] ?? { sample_count: 0, win_rate: null, avg_return_pct: null, expected_value_pct: null, payoff_ratio: null, avg_excess_return_pct: null, avg_mfe_pct: null, avg_mae_pct: null }} />)}
                  </div>
                  <div className="decision-calibration">
                    <header><strong>20日分层校准</strong><span>A 是否明显优于 B/C，决定这套规则有没有用</span></header>
                    <div>
                      {(["A", "B", "C", "D"] as DecisionCode[]).map((code) => (
                        <article key={code}>
                          <Tag color={DECISION_META[code].color}>{DECISION_META[code].label}</Tag>
                          <strong className={metricTone(summary?.calibration_20d[code].expected_value_pct)}>{formatPct(summary?.calibration_20d[code].expected_value_pct)}</strong>
                          <span>{summary?.calibration_20d[code].sample_count ?? 0} 个成熟样本</span>
                          <small>{DECISION_META[code].description}</small>
                        </article>
                      ))}
                    </div>
                  </div>
                  <Alert
                    type="warning"
                    showIcon
                    message="样本成熟前不训练权重"
                    description="每一层至少积累约30个成熟样本后，再比较验证增益、选股增益和定价增益；沪深300同期数据缺失时，超额收益会明确显示为待形成。"
                  />
                </div>
              )
            },
            { key: "five-year", label: "预期差五年验证", children: <FiveYearValidationPanel /> }
          ]}
        />
      </section>
    </main>
  );
}
