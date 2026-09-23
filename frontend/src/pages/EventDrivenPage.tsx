import {
  AimOutlined,
  CalendarOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  FireOutlined,
  GlobalOutlined,
  HistoryOutlined,
  LinkOutlined,
  SafetyCertificateOutlined
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Progress,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
  message
} from "antd";
import { useMemo, useState } from "react";
import type { DecisionCode, DecisionPolicyEvaluation, PolicyGateState } from "../api/decisionReview";
import {
  deleteFutureEvent,
  listFutureEventFeedback,
  listFutureEvents,
  type FutureEventFeedbackItem,
  type FutureEventItem,
  type FutureEventStage
} from "../api/futureEvents";
import DecisionPolicyPanel, { DecisionPolicyInline } from "../components/DecisionPolicyPanel";

type Horizon = "14" | "30" | "90" | "all";
type Opportunity = "可埋伏" | "重点跟踪" | "观察线索" | "兑现/回避" | "只观察";
type OpportunityFilter = "全部" | Opportunity;

const { TextArea } = Input;

const deleteReasonOptions = [
  "与A股关联弱",
  "时间不明确",
  "影响太弱",
  "市场已充分交易",
  "来源不可靠",
  "重复事件",
  "与投资无关",
  "其他"
].map((value) => ({ value, label: value }));

interface EventScore {
  total: number;
  certainty: number;
  mapping: number;
  expectationGap: number;
  timing: number;
  verification: number;
}

interface EventView {
  event: FutureEventItem;
  days: number | null;
  phase: "待确认" | "发现期" | "埋伏期" | "临近催化" | "兑现窗口" | "事后复盘";
  opportunity: Opportunity;
  score: EventScore;
  policyEvaluation: DecisionPolicyEvaluation;
}

const stageLabel: Record<FutureEventStage, string> = {
  clue: "待确认线索",
  window: "时间窗口",
  date_locked: "日期锁定",
  time_locked: "时刻锁定",
  completed: "已经兑现"
};

const sourceLabel: Record<FutureEventItem["source_status"], string> = {
  official: "官方确定",
  media: "媒体预告",
  zsxq: "知识星球线索",
  xueqiu: "雪球线索",
  mixed: "多源确认",
  unverified: "尚未确认"
};

const pricedInLabel: Record<FutureEventItem["priced_in_status"], string> = {
  no: "尚未交易",
  partial: "部分交易",
  yes: "充分交易",
  unknown: "等待判断"
};

const opportunityColor: Record<Opportunity, string> = {
  可埋伏: "green",
  重点跟踪: "blue",
  观察线索: "gold",
  "兑现/回避": "red",
  只观察: "default"
};

const phaseColor: Record<EventView["phase"], string> = {
  待确认: "default",
  发现期: "cyan",
  埋伏期: "green",
  临近催化: "orange",
  兑现窗口: "red",
  事后复盘: "purple"
};

function dayIndex(value: string): number {
  const [year, month, day] = value.split("-").map(Number);
  return Math.floor(Date.UTC(year, month - 1, day) / 86_400_000);
}

function isAShareMapping(stock: FutureEventItem["stock_mappings"][number]): boolean {
  const market = stock.market.trim().toUpperCase();
  const code = stock.code.trim().toUpperCase();
  const marketTokens = market.split(/[\/,+\s]+/).filter(Boolean);
  return marketTokens.some((item) => ["A", "SH", "SZ", "BJ", "SSE", "SZSE", "BSE"].includes(item))
    || /^(SH|SZ|BJ)?(60|68|00|30|83|87|92)\d{4}$/.test(code);
}

function daysUntil(event: FutureEventItem, today: string): number | null {
  if (event.stage === "clue") return null;
  const start = dayIndex(event.start_date);
  const now = dayIndex(today);
  if (start <= now && event.end_date && dayIndex(event.end_date) >= now) return 0;
  return start - now;
}

function eventPhase(event: FutureEventItem, days: number | null): EventView["phase"] {
  if (event.stage === "clue") return "待确认";
  if (event.stage === "completed" || (days !== null && days < 0)) return "事后复盘";
  if (days === null || days > 30) return "发现期";
  if (days >= 8) return "埋伏期";
  if (days >= 3) return "临近催化";
  return "兑现窗口";
}

function scoreEvent(event: FutureEventItem, days: number | null): EventScore {
  const stagePoints: Record<FutureEventStage, number> = {
    clue: 3,
    window: 9,
    date_locked: 14,
    time_locked: 15,
    completed: 2
  };
  const sourcePoints: Record<FutureEventItem["source_status"], number> = {
    official: 10,
    mixed: 9,
    media: 6,
    zsxq: 3,
    xueqiu: 3,
    unverified: 0
  };
  const certainty = Math.min(25, stagePoints[event.stage] + sourcePoints[event.source_status]);

  const aShareMappings = event.stock_mappings.filter((item) => (item.name || item.code) && isAShareMapping(item));
  const stockCount = aShareMappings.length;
  const stockPoints = stockCount === 0 ? 0 : stockCount === 1 ? 9 : stockCount === 2 ? 14 : stockCount === 3 ? 17 : 20;
  const hasClearRole = aShareMappings.some((item) => item.role.trim().length > 0);
  const mapping = stockCount === 0 ? 0 : Math.min(25, stockPoints + (event.impact_chain ? 3 : 0) + (hasClearRole ? 2 : 0));

  const gapPoints: Record<FutureEventItem["priced_in_status"], number> = {
    no: 25,
    partial: 16,
    unknown: 11,
    yes: 2
  };
  const expectationGap = gapPoints[event.priced_in_status];

  let timing = 3;
  if (days !== null) {
    if (days < 0) timing = 0;
    else if (days >= 8 && days <= 30) timing = 15;
    else if (days > 30 && days <= 60) timing = 12;
    else if (days >= 3) timing = 11;
    else if (days <= 2) timing = 5;
    else timing = 7;
  }

  const verification = Math.min(6, event.validation_points.length * 2)
    + Math.min(4, event.invalidation_conditions.length * 2);

  return {
    total: certainty + mapping + expectationGap + timing + verification,
    certainty,
    mapping,
    expectationGap,
    timing,
    verification
  };
}

function opportunityFor(event: FutureEventItem, days: number | null, score: EventScore): Opportunity {
  if (event.stage === "clue") return "观察线索";
  if (event.stage === "completed" || event.priced_in_status === "yes" || (days !== null && days < 0)) return "兑现/回避";
  if (days !== null && days <= 2) return "兑现/回避";
  if (!event.stock_mappings.some(isAShareMapping)) return "只观察";
  if (score.total >= 68 && days !== null && days >= 3) return "可埋伏";
  if (score.total >= 54) return "重点跟踪";
  return "只观察";
}

function eventPolicyEvaluation(
  event: FutureEventItem,
  days: number | null,
  baselineOpportunity: Opportunity
): DecisionPolicyEvaluation {
  const gate = (key: string, label: string, state: PolicyGateState, summary: string) => ({
    key,
    label,
    state,
    summary,
    facts: []
  });
  const aShareMappings = event.stock_mappings.filter((item) => (item.name || item.code) && isAShareMapping(item));
  const hasClearRole = aShareMappings.some((item) => {
    const role = item.role.trim();
    return role.length >= 4 && !/(映射|待验证|概念|参股|间接)/.test(role);
  });
  const evidenceState: PolicyGateState = event.source_status === "unverified"
    ? "fail"
    : event.source_urls.length && ["official", "mixed"].includes(event.source_status)
      ? "pass"
      : "wait";
  const industryState: PolicyGateState = event.impact_chain?.trim() ? "pass" : "missing";
  const stockState: PolicyGateState = !aShareMappings.length ? "missing" : hasClearRole ? "pass" : "wait";
  const pricingState: PolicyGateState = event.priced_in_status === "no"
    ? "pass"
    : event.priced_in_status === "yes" ? "fail" : "wait";
  const timingState: PolicyGateState = event.stage === "completed" || (days !== null && (days < 3))
    ? "fail"
    : ["date_locked", "time_locked"].includes(event.stage) && days !== null && days <= 30
      ? "pass"
      : event.stage === "clue" ? "missing" : "wait";
  const gates = [
    gate("evidence", "证据", evidenceState, evidenceState === "pass" ? "官方或多源确认且保留原始链接。" : "事件来源仍不足以作为硬事实。"),
    gate("industry", "传导链", industryState, industryState === "pass" ? "已写明影响传导。" : "缺少从事件到产业利润的传导链。"),
    gate("stock", "股票表达", stockState, stockState === "pass" ? "至少一项A股映射写明直接角色。" : "A股映射或直接受益角色仍待确认。"),
    gate("pricing", "定价", pricingState, `当前记录：${pricedInLabel[event.priced_in_status]}。`),
    gate("timing", "时点", timingState, `当前阶段：${stageLabel[event.stage]}，${days === null ? "日期未锁定" : `距离${days}天`}。`)
  ];
  let qqRecommendation: DecisionCode;
  if ([evidenceState, industryState, stockState, pricingState, timingState].includes("fail")) qqRecommendation = "D";
  else if ([evidenceState, industryState, stockState].some((state) => state !== "pass")) qqRecommendation = "C";
  else if (pricingState !== "pass" || timingState !== "pass") qqRecommendation = "B";
  else qqRecommendation = "A";
  const baselineRecommendation: DecisionCode = baselineOpportunity === "可埋伏"
    ? "A"
    : baselineOpportunity === "重点跟踪" ? "B"
      : baselineOpportunity === "兑现/回避" ? "D" : "C";
  const qualityChecks = [
    { key: "source", label: "原始来源", passed: event.source_urls.length > 0, message: event.source_urls.length ? "已记录" : "缺少可追溯链接" },
    { key: "date", label: "事件日期", passed: event.stage !== "clue", message: event.stage !== "clue" ? "已记录" : "日期尚未锁定" },
    { key: "impact", label: "传导逻辑", passed: Boolean(event.impact_chain?.trim()), message: event.impact_chain?.trim() ? "已记录" : "缺少事件传导逻辑" },
    { key: "validation", label: "升级条件", passed: event.validation_points.length > 0, message: event.validation_points.length ? "已记录" : "缺少升级条件" },
    { key: "invalidation", label: "证伪条件", passed: event.invalidation_conditions.length > 0, message: event.invalidation_conditions.length ? "已记录" : "缺少证伪条件" }
  ];
  return {
    policies: [
      {
        policy_id: "qq_2_1",
        version: "qq-2.1-experimental-2026-07-23",
        label: "qq 2.1 实验策略",
        recommendation: qqRecommendation,
        reason: gates.find((item) => item.state !== "pass")?.summary || "当前五项均满足实验口径。",
        scope: "事件驱动适配仍需单独验证，不能沿用其他信号的胜率。",
        gates
      },
      {
        policy_id: "simple_baseline",
        version: "event-score-baseline-2026-07-23",
        label: "旧排序分基线",
        recommendation: baselineRecommendation,
        reason: `沿用原研究分与时间阈值，得到“${baselineOpportunity}”。`,
        scope: "只用于保持原页面排序，并作为对照组。",
        gates: []
      }
    ],
    disagreement: qqRecommendation !== baselineRecommendation,
    quality_checks: qualityChecks,
    quality_complete: qualityChecks.every((item) => item.passed),
    principle: "两套策略只作对照，不代表事件必涨，也不自动升级为交易结论。"
  };
}

function buildView(event: FutureEventItem, today: string): EventView {
  const days = daysUntil(event, today);
  const score = scoreEvent(event, days);
  const opportunity = opportunityFor(event, days, score);
  return {
    event,
    days,
    phase: eventPhase(event, days),
    opportunity,
    score,
    policyEvaluation: eventPolicyEvaluation(event, days, opportunity)
  };
}

function countdownText(view: EventView): string {
  if (view.phase === "待确认") return "等待官宣";
  if (view.days === null) return "时间待定";
  if (view.days < 0) return `已过 ${Math.abs(view.days)} 天`;
  if (view.days === 0) return "今天兑现";
  return `还有 ${view.days} 天`;
}

function nextAction(view: EventView): string {
  if (view.opportunity === "观察线索") return "先等官方日期、任务公告或权威日程；未升级前不进入埋伏池。";
  if (view.opportunity === "可埋伏") return "建立事件观察篮子，核心受益与高弹性分开；等待板块首次有效表达再升级。";
  if (view.opportunity === "重点跟踪") return "补齐A股受益链和盘面证据；只有预期差与图形同时成立才进入埋伏池。";
  if (view.opportunity === "兑现/回避") return "进入兑现纪律：不只因日期追高，重点检查利好落地、冲高回落与资金提前撤退。";
  return "保留日历提醒，暂不占用交易研究精力。";
}

function eventDateText(event: FutureEventItem): string {
  if (event.stage === "clue") return "日期待官方确认";
  const range = event.end_date && event.end_date !== event.start_date
    ? `${event.start_date} 至 ${event.end_date}`
    : event.start_date;
  return event.exact_time ? `${range} · ${event.exact_time}` : range;
}

function feedbackTimeText(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function ScoreBreakdown({ score }: { score: EventScore }) {
  const items = [
    ["事件确定性", score.certainty, 25],
    ["A股映射", score.mapping, 25],
    ["预期差", score.expectationGap, 25],
    ["时间位置", score.timing, 15],
    ["验证完整度", score.verification, 10]
  ] as const;
  return (
    <div className="event-drive-score-breakdown">
      {items.map(([label, value, max]) => (
        <div key={label}>
          <span>{label}</span>
          <Progress percent={Math.round((value / max) * 100)} showInfo={false} size="small" />
          <strong>{value}/{max}</strong>
        </div>
      ))}
    </div>
  );
}

function EventDriveCard({
  view,
  onOpen,
  onDelete,
  deleting
}: {
  view: EventView;
  onOpen: () => void;
  onDelete: () => void;
  deleting: boolean;
}) {
  const { event } = view;
  const stocks = event.stock_mappings.filter((item) => (item.name || item.code) && isAShareMapping(item)).slice(0, 4);
  return (
    <Card
      hoverable
      className={`event-drive-card is-${view.opportunity.replace("/", "-")}`}
      onClick={onOpen}
      role="button"
      tabIndex={0}
      onKeyDown={(keyEvent) => {
        if (keyEvent.key === "Enter" || keyEvent.key === " ") onOpen();
      }}
    >
      <div className="event-drive-card-head">
        <Space size={5} wrap>
          <Tag color={opportunityColor[view.opportunity]}>{view.opportunity}</Tag>
          <Tag color={phaseColor[view.phase]}>{view.phase}</Tag>
          <Tag>{event.priority}级</Tag>
        </Space>
        <div onClick={(clickEvent) => clickEvent.stopPropagation()}>
          <Button type="text" danger size="small" loading={deleting} icon={<DeleteOutlined />} aria-label={`删除 ${event.title}`} onClick={onDelete} />
        </div>
      </div>

      <div className="event-drive-card-main">
        <div>
          <Typography.Title level={4}>{event.title}</Typography.Title>
          <Typography.Text type="secondary" className="event-drive-card-date">
            <CalendarOutlined /> {eventDateText(event)}
          </Typography.Text>
        </div>
        <div className="event-drive-score">
          <strong>{view.score.total}</strong>
          <span>旧排序分</span>
        </div>
      </div>
      <DecisionPolicyInline evaluation={view.policyEvaluation} />

      <div className="event-drive-countdown">
        <ClockCircleOutlined />
        <strong>{countdownText(view)}</strong>
        <span>· {pricedInLabel[event.priced_in_status]}</span>
      </div>

      <Typography.Paragraph className="event-drive-thesis" ellipsis={{ rows: 2 }}>
        {event.impact_chain || event.source_summary || "等待补充事件传导逻辑"}
      </Typography.Paragraph>

      <div className="event-drive-stock-row">
        {stocks.length ? stocks.map((stock, index) => (
          <Tag key={`${stock.market}-${stock.code}-${index}`}>
            {stock.name || stock.code}{stock.role ? ` · ${stock.role}` : ""}
          </Tag>
        )) : <Typography.Text type="secondary">暂无明确A股承接，只作跨市场观察</Typography.Text>}
      </div>

      <div className="event-drive-card-foot">
        <span>{event.category} · {sourceLabel[event.source_status]}</span>
        <strong>查看埋伏方案 →</strong>
      </div>
    </Card>
  );
}

function EventDriveDetail({ view }: { view: EventView }) {
  const { event } = view;
  const stocks = event.stock_mappings.filter((item) => (item.name || item.code) && isAShareMapping(item));
  const referenceStocks = event.stock_mappings.filter((item) => (item.name || item.code) && !isAShareMapping(item));
  return (
    <div className="event-drive-detail">
      <Alert
        type={view.opportunity === "可埋伏" ? "success" : view.opportunity === "兑现/回避" ? "warning" : "info"}
        showIcon
        message={`旧排序模型动作：${view.opportunity}`}
        description={`${nextAction(view)} 这只是对照基线，不是确定交易结论。`}
      />
      <DecisionPolicyPanel evaluation={view.policyEvaluation} />

      <div className="event-drive-decision-grid">
        <div><span>当前阶段</span><strong>{view.phase}</strong></div>
        <div><span>倒计时</span><strong>{countdownText(view)}</strong></div>
        <div><span>市场消化</span><strong>{pricedInLabel[event.priced_in_status]}</strong></div>
        <div><span>证据状态</span><strong>{stageLabel[event.stage]} · {sourceLabel[event.source_status]}</strong></div>
      </div>

      <section className="event-drive-detail-section">
        <div className="event-drive-section-title"><AimOutlined /><strong>为什么市场可能交易它</strong></div>
        <div className="event-drive-detail-copy">
          <div><span>消息依据</span><p>{event.source_summary || "等待补充"}</p></div>
          <div><span>传导逻辑</span><p>{event.impact_chain || "等待补充"}</p></div>
          <div><span>已有表达</span><p>{event.price_expression || "尚未记录盘面表达"}</p></div>
        </div>
      </section>

      <section className="event-drive-detail-section">
        <div className="event-drive-section-title"><FireOutlined /><strong>A股受益链：先核心，再弹性</strong></div>
        {stocks.length ? (
          <div className="event-drive-stock-grid">
            {stocks.map((stock, index) => (
              <div key={`${stock.market}-${stock.code}-${index}`}>
                <strong>{stock.name || stock.code}</strong>
                <span>{[stock.market, stock.code].filter(Boolean).join(" · ")}</span>
                <p>{stock.role || "角色待确认"}</p>
              </div>
            ))}
          </div>
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未建立A股受益链" />}
        {referenceStocks.length ? (
          <div className="event-drive-reference-row">
            <Typography.Text type="secondary">跨市场参照</Typography.Text>
            <Space size={[5, 5]} wrap>
              {referenceStocks.map((stock, index) => (
                <Tag key={`${stock.market}-${stock.code}-${index}`}>{stock.name || stock.code}{stock.role ? ` · ${stock.role}` : ""}</Tag>
              ))}
            </Space>
          </div>
        ) : null}
      </section>

      <section className="event-drive-detail-section event-drive-plan">
        <div className="event-drive-section-title"><CheckCircleOutlined /><strong>执行清单</strong></div>
        <div className="event-drive-plan-grid">
          <div className="is-now"><span>现在做什么</span><p>{nextAction(view)}</p></div>
          <div className="is-confirm"><span>升级触发器</span><ul>{event.validation_points.length ? event.validation_points.map((item) => <li key={item}>{item}</li>) : <li>补充官方日期与板块首次有效表达</li>}</ul></div>
          <div className="is-exit"><span>退出 / 证伪</span><ul>{event.invalidation_conditions.length ? event.invalidation_conditions.map((item) => <li key={item}>{item}</li>) : <li>事件延期、取消，或盘面已充分交易</li>}</ul></div>
        </div>
      </section>

      <section className="event-drive-detail-section">
        <div className="event-drive-section-title"><SafetyCertificateOutlined /><strong>旧排序分拆解</strong></div>
        <ScoreBreakdown score={view.score} />
        <Typography.Text type="secondary">旧排序分只用于保留原页面优先级，并作为策略对照；不代表上涨概率或目标涨幅。</Typography.Text>
      </section>

      {event.stage_history.length ? (
        <section className="event-drive-detail-section">
          <div className="event-drive-section-title"><ClockCircleOutlined /><strong>事件锁定过程</strong></div>
          <div className="event-drive-history">
            {event.stage_history.map((item, index) => (
              <span key={`${item.stage}-${item.at}-${index}`}><CheckCircleOutlined /> {item.at} · {stageLabel[item.stage]}{item.note ? `：${item.note}` : ""}</span>
            ))}
          </div>
        </section>
      ) : null}

      {event.source_urls.length ? (
        <Space size={14} wrap>
          {event.source_urls.map((url, index) => <a href={url} target="_blank" rel="noreferrer" key={`${url}-${index}`}><LinkOutlined /> 原始来源 {index + 1}</a>)}
        </Space>
      ) : null}
    </div>
  );
}

export default function EventDrivenPage() {
  const [horizon, setHorizon] = useState<Horizon>("30");
  const [opportunityFilter, setOpportunityFilter] = useState<OpportunityFilter>("全部");
  const [category, setCategory] = useState<string>();
  const [selectedEventId, setSelectedEventId] = useState<number | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<EventView | null>(null);
  const [deleteReasonCategory, setDeleteReasonCategory] = useState<string>();
  const [deleteReason, setDeleteReason] = useState("");
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackKeyword, setFeedbackKeyword] = useState("");
  const [feedbackReason, setFeedbackReason] = useState<string>();
  const [feedbackCategory, setFeedbackCategory] = useState<string>();
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const events = useQuery({
    queryKey: ["future-events", "event-driven"],
    queryFn: () => listFutureEvents("all"),
    refetchInterval: 60_000
  });
  const feedback = useQuery({
    queryKey: ["future-event-feedback"],
    queryFn: listFutureEventFeedback,
    enabled: feedbackOpen
  });

  const removeEvent = useMutation({
    mutationFn: ({ eventId, reasonCategory, reason }: { eventId: number; reasonCategory?: string; reason?: string }) =>
      deleteFutureEvent(eventId, { reason_category: reasonCategory, reason }),
    onSuccess: () => {
      setSelectedEventId(null);
      setDeleteTarget(null);
      setDeleteReasonCategory(undefined);
      setDeleteReason("");
      messageApi.success("已删除，原因和事件快照已写入日志");
      void Promise.allSettled([
        queryClient.invalidateQueries({ queryKey: ["future-events"] }),
        queryClient.invalidateQueries({ queryKey: ["future-event-feedback"] })
      ]);
    },
    onError: (error) => messageApi.error(`删除失败：${String(error)}`)
  });

  const allViews = useMemo(() => {
    const data = events.data;
    if (!data) return [];
    const today = data.today;
    const opportunityOrder: Record<Opportunity, number> = {
      可埋伏: 0,
      重点跟踪: 1,
      观察线索: 2,
      只观察: 3,
      "兑现/回避": 4
    };
    return data.items
      .filter((event) => event.source_status !== "unverified" && event.source_urls.length > 0)
      .map((event) => buildView(event, today))
      .sort((left, right) => opportunityOrder[left.opportunity] - opportunityOrder[right.opportunity]
        || right.score.total - left.score.total
        || (left.days ?? 9_999) - (right.days ?? 9_999));
  }, [events.data]);

  const categories = useMemo(() => [...new Set(allViews.map((item) => item.event.category))].sort(), [allViews]);
  const feedbackCategories = useMemo(
    () => [...new Set((feedback.data ?? []).map((item) => item.category))].sort(),
    [feedback.data]
  );
  const filteredFeedback = useMemo(() => {
    const keyword = feedbackKeyword.trim().toLowerCase();
    return (feedback.data ?? []).filter((item) =>
      (!keyword || item.title.toLowerCase().includes(keyword) || (item.note ?? "").toLowerCase().includes(keyword))
      && (!feedbackReason || item.reason_category === feedbackReason)
      && (!feedbackCategory || item.category === feedbackCategory)
    );
  }, [feedback.data, feedbackCategory, feedbackKeyword, feedbackReason]);
  const visibleViews = useMemo(() => allViews.filter((view) => {
    const withinHorizon = horizon === "all"
      || view.days === null
      || (view.days >= 0 && view.days <= Number(horizon));
    return withinHorizon
      && (opportunityFilter === "全部" || view.opportunity === opportunityFilter)
      && (!category || view.event.category === category);
  }), [allViews, category, horizon, opportunityFilter]);

  const selectedView = allViews.find((view) => view.event.id === selectedEventId) ?? null;
  const actionableCount = allViews.filter((view) => view.opportunity === "可埋伏").length;
  const nearCount = allViews.filter((view) => view.days !== null && view.days >= 0 && view.days <= 7).length;
  const clueCount = allViews.filter((view) => view.opportunity === "观察线索").length;
  const pricedCount = allViews.filter((view) => view.event.priced_in_status === "yes" || view.opportunity === "兑现/回避").length;
  const phaseCounts = ["发现期", "埋伏期", "临近催化", "兑现窗口"].map((phase) => ({
    phase,
    count: allViews.filter((view) => view.phase === phase).length
  }));

  return (
    <main className="page event-driven-page">
      {contextHolder}
      <header className="event-driven-header">
        <div>
          <Typography.Title level={2}>事件驱动</Typography.Title>
          <Typography.Text type="secondary">按时间、催化状态和盘面反应管理事件。</Typography.Text>
        </div>
        <Space wrap>
          <Button icon={<HistoryOutlined />} onClick={() => setFeedbackOpen(true)}>删除日志</Button>
          <Button
            icon={<GlobalOutlined />}
            onClick={() => events.refetch().then(() => messageApi.success("已同步本对话写入的最新网络检索结果"))}
            loading={events.isFetching}
          >
            同步对话结果
          </Button>
        </Space>
      </header>

      <Alert
        className="event-driven-rule"
        type="info"
        showIcon
        message="旧模型与qq策略仅作对照；没有官方日期的线索只进入观察池。"
      />

      <div className="event-driven-stats">
        <Card><Statistic title="旧模型可埋伏" value={actionableCount} suffix="项" /></Card>
        <Card><Statistic title="7天内催化" value={nearCount} suffix="项" /></Card>
        <Card><Statistic title="等待官宣" value={clueCount} suffix="项" /></Card>
        <Card><Statistic title="兑现 / 回避" value={pricedCount} suffix="项" /></Card>
      </div>

      <section className="panel event-driven-rhythm">
        <div className="event-drive-section-title"><ClockCircleOutlined /><strong>埋伏节奏</strong><span>越靠近兑现日，越要从找机会切换为防止预期兑现。</span></div>
        <div className="event-driven-rhythm-track">
          {phaseCounts.map((item, index) => (
            <div key={item.phase} className={`phase-${index + 1}`}>
              <span>{index + 1}</span>
              <strong>{item.phase}</strong>
              <small>{item.count} 项</small>
            </div>
          ))}
        </div>
      </section>

      <div className="event-driven-toolbar">
        <Segmented<Horizon>
          value={horizon}
          onChange={setHorizon}
          options={[
            { label: "未来14天", value: "14" },
            { label: "未来30天", value: "30" },
            { label: "未来90天", value: "90" },
            { label: "全部", value: "all" }
          ]}
        />
        <Segmented<OpportunityFilter>
          value={opportunityFilter}
          onChange={setOpportunityFilter}
          options={["全部", "可埋伏", "重点跟踪", "观察线索", "兑现/回避"]}
        />
        <Select
          allowClear
          value={category}
          placeholder="事件类型"
          onChange={setCategory}
          options={categories.map((value) => ({ value, label: value }))}
        />
        <Typography.Text type="secondary">显示 {visibleViews.length} / {allViews.length} 项</Typography.Text>
      </div>

      {events.isError ? <Alert type="error" showIcon message="事件数据暂时不可用" description={String(events.error)} /> : null}
      {events.isLoading ? (
        <div className="page-loading"><Spin size="small" /><Typography.Text type="secondary">正在整理事件机会</Typography.Text></div>
      ) : visibleViews.length ? (
        <div className="event-driven-grid">
          {visibleViews.map((view) => (
            <EventDriveCard
              key={view.event.id}
              view={view}
              onOpen={() => setSelectedEventId(view.event.id)}
              onDelete={() => {
                setDeleteTarget(view);
                setDeleteReasonCategory(undefined);
                setDeleteReason("");
              }}
              deleting={removeEvent.isPending && removeEvent.variables?.eventId === view.event.id}
            />
          ))}
        </div>
      ) : (
        <div className="panel event-driven-empty">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下没有事件；可扩大日期范围或切回全部机会。" />
        </div>
      )}

      <Modal
        open={Boolean(selectedView)}
        width={920}
        footer={null}
        destroyOnClose
        className="event-driven-modal"
        onCancel={() => setSelectedEventId(null)}
        title={selectedView ? (
          <Space size={6} wrap>
            <Tag color={opportunityColor[selectedView.opportunity]}>{selectedView.opportunity}</Tag>
            <span>{selectedView.event.title}</span>
          </Space>
        ) : null}
      >
        {selectedView ? <EventDriveDetail view={selectedView} /> : null}
      </Modal>

      <Modal
        open={Boolean(deleteTarget)}
        title={deleteTarget ? `删除「${deleteTarget.event.title}」` : "删除事件"}
        okText="删除并写入日志"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        confirmLoading={removeEvent.isPending}
        onCancel={() => {
          if (!removeEvent.isPending) setDeleteTarget(null);
        }}
        onOk={() => deleteTarget && removeEvent.mutate({
          eventId: deleteTarget.event.id,
          reasonCategory: deleteReasonCategory,
          reason: deleteReason.trim() || undefined
        })}
      >
        <div className="event-delete-form">
          <Typography.Text type="secondary">删除后会保留完整事件快照，并阻止相同事件自动回填。原因可以不填。</Typography.Text>
          <label>
            <span>原因分类（可选）</span>
            <Select
              allowClear
              value={deleteReasonCategory}
              placeholder="选择一个原因，方便以后筛选"
              options={deleteReasonOptions}
              onChange={setDeleteReasonCategory}
            />
          </label>
          <label>
            <span>补充说明（可选）</span>
            <TextArea
              value={deleteReason}
              maxLength={500}
              showCount
              autoSize={{ minRows: 3, maxRows: 6 }}
              placeholder="例如：只有概念映射，没有直接订单；时间太远，暂时不看"
              onChange={(event) => setDeleteReason(event.target.value)}
            />
          </label>
        </div>
      </Modal>

      <Modal
        open={feedbackOpen}
        title={<Space><HistoryOutlined /><span>事件删除日志</span><Tag>{feedback.data?.length ?? 0} 条</Tag></Space>}
        width={1060}
        footer={null}
        destroyOnClose
        onCancel={() => setFeedbackOpen(false)}
      >
        <div className="event-feedback-log">
          <div className="event-feedback-filters">
            <Input
              allowClear
              value={feedbackKeyword}
              placeholder="搜索事件标题或删除说明"
              onChange={(event) => setFeedbackKeyword(event.target.value)}
            />
            <Select
              allowClear
              value={feedbackReason}
              placeholder="删除原因"
              options={deleteReasonOptions}
              onChange={setFeedbackReason}
            />
            <Select
              allowClear
              value={feedbackCategory}
              placeholder="事件类型"
              options={feedbackCategories.map((value) => ({ value, label: value }))}
              onChange={setFeedbackCategory}
            />
            <Typography.Text type="secondary">筛选结果 {filteredFeedback.length} 条</Typography.Text>
          </div>
          <Table<FutureEventFeedbackItem>
            rowKey="id"
            loading={feedback.isLoading}
            dataSource={filteredFeedback}
            pagination={{ pageSize: 10, showSizeChanger: false }}
            scroll={{ x: 920 }}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配的删除记录" /> }}
            columns={[
              { title: "删除时间", dataIndex: "created_at", width: 170, render: feedbackTimeText },
              { title: "事件", dataIndex: "title", width: 260, render: (value: string, item) => <div className="event-feedback-title"><strong>{value}</strong><span>{item.priority}级 · {stageLabel[item.stage as FutureEventStage] ?? item.stage}</span></div> },
              { title: "类型", dataIndex: "category", width: 110, render: (value: string) => <Tag>{value}</Tag> },
              { title: "删除原因", dataIndex: "reason_category", width: 140, render: (value: string | null) => value ? <Tag color="orange">{value}</Tag> : <Tag>未分类</Tag> },
              { title: "补充说明", dataIndex: "note", render: (value: string | null) => value || <Typography.Text type="secondary">未填写</Typography.Text> }
            ]}
          />
        </div>
      </Modal>
    </main>
  );
}
