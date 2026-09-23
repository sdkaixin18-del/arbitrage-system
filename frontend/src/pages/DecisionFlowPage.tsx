import {
  ArrowRightOutlined,
  CheckCircleFilled,
  ClockCircleFilled,
  ReloadOutlined,
  StopFilled
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Spin, Tag, Typography, message } from "antd";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  decisionFlowApi,
  type DecisionCode,
  type DecisionFlowGate,
  type DecisionFlowItem
} from "../api/decisionFlow";

const DECISION_META: Record<DecisionCode, { title: string; short: string }> = {
  A: { title: "进入交易计划", short: "可执行" },
  B: { title: "等待价格 / 触发", short: "等触发" },
  C: { title: "等待信息确认", short: "待验证" },
  D: { title: "剔除", short: "不参与" }
};

function percent(value?: number) {
  if (value === undefined || value === null || !Number.isFinite(value)) return "-";
  return `${value > 0 ? "+" : ""}${(value * 100).toFixed(0)}%`;
}

function gateIcon(gate: DecisionFlowGate) {
  if (gate.status === "pass") return <CheckCircleFilled />;
  if (gate.status === "fail") return <StopFilled />;
  return <ClockCircleFilled />;
}

function DecisionCard({ item }: { item: DecisionFlowItem }) {
  const navigate = useNavigate();
  const failedOrWaiting = item.gates.filter((gate) => gate.status !== "pass");
  const discoveryGates = item.gates.filter((gate) => gate.layer === "discover");
  const confirmationGates = item.gates.filter((gate) => gate.layer === "confirm");

  const renderGateGroup = (label: string, gates: DecisionFlowGate[]) => {
    if (!gates.length) return null;
    return (
      <div className="decision-flow-gate-group">
        <small>{label}</small>
        <div className="decision-flow-gates">
          {gates.map((gate) => (
            <span key={gate.key} className={`gate-${gate.status}`}>
              {gateIcon(gate)}
              {gate.label}
            </span>
          ))}
        </div>
      </div>
    );
  };

  return (
    <article className={`decision-flow-card decision-${item.decision_code.toLowerCase()}`}>
      <div className="decision-flow-card-head">
        <div className="decision-flow-card-title">
          <span className="decision-code">{item.decision_code}</span>
          <div>
            <div className="decision-flow-card-eyebrow">
              <Tag bordered={false}>{item.source}</Tag>
              <span>{item.cycle}</span>
              <span>{item.pricing_status}</span>
            </div>
            <Typography.Title level={4}>{item.name}</Typography.Title>
            {item.primary_company ? <small>首选表达：{item.primary_company}</small> : null}
          </div>
        </div>
        <Button type="text" onClick={() => navigate(item.link)}>
          {item.link_label} <ArrowRightOutlined />
        </Button>
      </div>

      <p className="decision-flow-card-action">{item.current_action}</p>

      <div className="decision-flow-layer-gates" aria-label="分层决策门">
        {renderGateGroup("发现层", discoveryGates)}
        {renderGateGroup("确认层", confirmationGates)}
      </div>

      <div className="decision-flow-next">
        <div>
          <span>下一触发</span>
          <strong>{item.trigger}</strong>
        </div>
        <div>
          <span>失效条件</span>
          <strong>{item.invalidation}</strong>
        </div>
      </div>

      <details className="decision-flow-details">
        <summary>
          查看依据
          {failedOrWaiting.length ? ` · 仍有 ${failedOrWaiting.length} 项未通过` : " · 两层条件已通过"}
        </summary>
        <ul>
          {item.facts.map((fact) => <li key={fact}>{fact}</li>)}
        </ul>
      </details>
    </article>
  );
}

export default function DecisionFlowPage() {
  const queryClient = useQueryClient();
  const [activeCode, setActiveCode] = useState<DecisionCode>("A");
  const overview = useQuery({
    queryKey: ["decision-flow"],
    queryFn: () => decisionFlowApi.overview(),
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false
  });
  const refresh = useMutation({
    mutationFn: () => decisionFlowApi.overview(true),
    onSuccess: (data) => {
      queryClient.setQueryData(["decision-flow"], data);
      message.success("今日决策已重新生成");
    },
    onError: () => message.error("刷新失败，已保留上次结果")
  });

  useEffect(() => {
    const counts = overview.data?.counts;
    if (!counts || counts[activeCode]) return;
    const next = (["A", "B", "C", "D"] as DecisionCode[]).find((code) => counts[code] > 0);
    if (next) setActiveCode(next);
  }, [overview.data?.counts, activeCode]);

  if (overview.isLoading) {
    return (
      <main className="page decision-flow-page">
        <div className="decision-flow-loading">
          <Spin />
          <strong>正在生成今日决策</strong>
          <span>依次计算发现层、确认层和仓位层，首次加载约需数秒。</span>
        </div>
      </main>
    );
  }

  if (overview.isError || !overview.data) {
    return (
      <main className="page decision-flow-page">
        <Alert
          type="error"
          showIcon
          message="今日决策暂时无法生成"
          description={String(overview.error || "接口没有返回数据")}
          action={<Button onClick={() => overview.refetch()}>重试</Button>}
        />
      </main>
    );
  }

  const data = overview.data;
  const queue = data.queues[activeCode] ?? [];

  return (
    <main className="page decision-flow-page">
      <header className="decision-flow-page-head">
        <div>
          <Typography.Title level={2}>今日决策</Typography.Title>
          <Typography.Text>收盘后只走三层：发现候选 → 确认证据 → 决定仓位。</Typography.Text>
        </div>
        <div className="decision-flow-head-actions">
          <span>数据截至 {data.as_of}</span>
          <Button
            icon={<ReloadOutlined />}
            loading={refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            重新生成
          </Button>
        </div>
      </header>

      {data.status === "partial" ? (
        <Alert
          type="warning"
          showIcon
          message="部分市场数据不可用"
          description={data.message}
        />
      ) : null}
      {data.data_status === "stale" ? (
        <Alert
          type="warning"
          showIcon
          message={`市场数据已滞后 ${data.lag_days} 天，A类结论不得直接执行`}
        />
      ) : null}

      <section className={`decision-flow-hero style-${data.headline.market_style}`}>
        <div className="decision-flow-hero-state">
          <span>仓位层 · 市场风格</span>
          <strong>{data.headline.market_style}</strong>
          <p>{data.headline.style_action}</p>
        </div>
        <div className="decision-flow-hero-budget">
          <span>建议风险预算</span>
          <strong>{data.headline.risk_budget}</strong>
        </div>
        <div className="decision-flow-hero-action">
          <span>今天只做什么</span>
          <strong>{data.headline.action}</strong>
        </div>
      </section>

      <section className="decision-flow-process" aria-label="决策流程">
        {data.steps.map((step, index) => (
          <div className="decision-flow-process-step" key={step.key}>
            <span>{step.number}</span>
            <div>
              <small>{step.label}</small>
              <strong>{step.status}</strong>
              <p>{step.detail}</p>
            </div>
            {index < data.steps.length - 1 ? <ArrowRightOutlined /> : null}
          </div>
        ))}
      </section>

      <section className="decision-flow-market-strip">
        <span>全市场成员广度 <b>{percent(data.market_metrics.market_member_breadth)}</b></span>
        <span>行业指数广度 <b>{percent(data.market_metrics.industry_index_breadth)}</b></span>
        <span>高低Beta 20日差 <b>{percent(data.market_metrics.high_low_beta_spread_20d)}</b></span>
        <p>{data.rule}</p>
      </section>

      <section className="decision-flow-queue">
        <div className="decision-flow-tabs" role="tablist" aria-label="决策状态">
          {(Object.keys(DECISION_META) as DecisionCode[]).map((code) => (
            <button
              key={code}
              type="button"
              role="tab"
              aria-selected={activeCode === code}
              className={`decision-flow-tab code-${code.toLowerCase()} ${activeCode === code ? "is-active" : ""}`}
              onClick={() => setActiveCode(code)}
            >
              <span>{code}</span>
              <div>
                <strong>{DECISION_META[code].short}</strong>
                <small>{data.counts[code]} 个</small>
              </div>
            </button>
          ))}
        </div>

        <div className="decision-flow-queue-heading">
          <div>
            <Typography.Title level={3}>{activeCode} · {DECISION_META[activeCode].title}</Typography.Title>
            <Typography.Text>{data.decision_definitions[activeCode]}</Typography.Text>
          </div>
          <b>{queue.length}</b>
        </div>

        <div className="decision-flow-card-list">
          {queue.length
            ? queue.map((item) => <DecisionCard key={item.key} item={item} />)
            : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`当前没有 ${activeCode} 类方向`} />}
        </div>
      </section>
    </main>
  );
}
