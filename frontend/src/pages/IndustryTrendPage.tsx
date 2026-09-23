import {
  ArrowRightOutlined,
  CalendarOutlined,
  DeleteOutlined,
  EditOutlined,
  HistoryOutlined,
  LinkOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SaveOutlined,
  SettingOutlined,
  StarFilled
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Progress,
  Segmented,
  Select,
  Space,
  Steps,
  Table,
  Tag,
  Timeline,
  Typography,
  message
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useRef, useState } from "react";
import { useBlocker, useLocation, useNavigate } from "react-router-dom";
import {
  industryTrendApi,
  type AttentionLevel,
  type CatalystStatus,
  type CausalEvidenceType,
  type CausalSignalStatus,
  type CausalStage,
  type CompanyExpectationGapGate,
  type CompanyExpectationAnalysis,
  type CompanyMarket,
  type ExpectationGapCalibration,
  type ExpectationGapStatus,
  type IndustryExpectationCompany,
  type IndustryExpectationSummary,
  type IndustryPhase,
  type IndustryCrossMarketBasket,
  type IndustryCrossMarketIntelligence,
  type IndustryCrossMarketProxy,
  type IndustryCrossMarketSector,
  type IndustryCompanyRecommendation,
  type IndustryIntelligence,
  type IndustryTrendCatalyst,
  type IndustryTrendCompany,
  type IndustryTrendDetail,
  type IndustryTrendDraft,
  type IndustryTrendNode,
  type IndustryTrendSource,
  type IndustryTrendUpdate,
  type IndustryTrendValidation,
  type InvestmentVerdict,
  type NodeType,
  type NodeMaturityStatus,
  type PricingStatus,
  type TrackingStatus,
  type VerificationStatus
} from "../api/industryTrend";
import DecisionPolicyPanel, { policyConsensusDefault } from "../components/DecisionPolicyPanel";

const { TextArea } = Input;

const PHASES: IndustryPhase[] = ["观察期", "萌芽期", "验证期", "增长期", "爆发期", "成熟期", "退潮期"];
const ATTENTION_LEVELS: AttentionLevel[] = ["重点跟踪", "持续跟踪", "观察", "暂停"];
const VERDICTS: InvestmentVerdict[] = ["通过", "观察", "否决"];
const PRICING_STATUSES: PricingStatus[] = ["未定价", "部分定价", "充分定价"];
const EXPECTATION_GAP_STATUSES: ExpectationGapStatus[] = ["正向预期差", "基本匹配", "负向预期差", "无法判断"];
const NODE_TYPES: NodeType[] = ["需求驱动", "网络与系统", "光互联产品", "核心器件", "制造与配套"];
const NODE_MATURITY_STATUSES: NodeMaturityStatus[] = ["前沿储备", "验证中", "小批量", "放量中", "成熟应用"];
type NodeTypeMeta = { title?: string; question: string; description: string };

const NODE_TYPE_META: Record<NodeType, NodeTypeMeta> = {
  需求驱动: { title: "需求起点", question: "谁在加预算？", description: "先确认需求是不是持续，而不是只看概念。" },
  网络与系统: { title: "网络架构", question: "预算先买什么？", description: "看GPU集群如何组网，决定铜还是光、用哪种速率。" },
  光互联产品: { title: "模块形态", question: "收入在哪兑现？", description: "模块、线缆和光引擎是最直接的订单载体。" },
  核心器件: { title: "模块BOM", question: "一个模块需要什么？", description: "把电芯片、光芯片、无源光学和结构散热拆开，寻找成本与供给瓶颈。" },
  制造与配套: { title: "封装与测试", question: "怎样组装并量产？", description: "贴片、键合、耦合、校准和测试共同决定良率与交付速度。" }
};
const PCB_NODE_TYPE_META: Record<NodeType, NodeTypeMeta> = {
  需求驱动: { title: "需求起点", question: "谁在扩AI算力？", description: "云厂商资本开支、AI服务器和高速交换端口先决定总需求。" },
  网络与系统: { title: "订单落点", question: "服务器板还是交换机板？", description: "先拆开两条订单线，不把所有AI PCB混成同一种产品。" },
  光互联产品: { title: "板级价值", question: "哪类板价值量提升？", description: "高多层、HDI和背板分别对应不同平台、工艺与供应商。" },
  核心器件: { title: "关键材料", question: "谁决定损耗和成本？", description: "CCL、电子布、铜箔和树脂决定高速性能、材料价格与供给瓶颈。" },
  制造与配套: { title: "量产兑现", question: "订单怎样变成利润？", description: "设备、客户认证、良率爬坡和海外交付决定利润能否兑现。" }
};
const CLOUD_NODE_TYPE_META: Record<NodeType, NodeTypeMeta> = {
  需求驱动: { title: "付费需求", question: "谁愿意为算力付钱？", description: "先确认模型公司、企业和政企客户是否真的增加云与算力预算。" },
  网络与系统: { title: "资源运营商", question: "谁买卡并运营资源池？", description: "云厂商、运营商和算力租赁平台先采购设备，再负责调度、计费与交付。" },
  光互联产品: { title: "收费产品", question: "算力怎样卖给客户？", description: "公有云、GPU租赁、裸金属、Token和SaaS是不同收费方式，不能混为同一种收入。" },
  核心器件: { title: "资产与交付", question: "出租的算力靠什么建成？", description: "芯片、服务器、机房、电力、网络和存储共同决定可出租规模与交付成本。" },
  制造与配套: { title: "利润兑现", question: "收入最后能不能变成现金？", description: "只看出租率、租赁单价、续租、折旧、毛利和现金流，不用签约金额替代利润。" }
};
const DOMESTIC_COMPUTE_NODE_TYPE_META: Record<NodeType, NodeTypeMeta> = {
  需求驱动: { title: "需求起点", question: "谁在增加国产算力预算？", description: "先确认政企、运营商和互联网客户的真实采购与扩容，不用政策口号代替订单。" },
  网络与系统: { title: "系统与集群", question: "算力怎样形成可用集群？", description: "服务器、存储、交换网络和调度软件共同决定芯片能否转化为有效算力。" },
  光互联产品: { title: "系统形态", question: "收入在哪种产品兑现？", description: "区分超节点、AI服务器、整机柜和算力服务，不把单一器件出货等同于系统收入。" },
  核心器件: { title: "核心器件", question: "哪些环节决定有效算力？", description: "CPU、GPU/DCU、HBM、交换芯片与高速互联共同约束性能、成本和国产化率。" },
  制造与配套: { title: "工程交付", question: "怎样稳定交付并持续运行？", description: "电源、液冷、机房、测试与运维决定集群能否按期上线和保持高利用率。" }
};
const SPACE_NODE_TYPE_META: Record<NodeType, NodeTypeMeta> = {
  需求驱动: { title: "星座与应用需求", question: "谁在持续买发射和连接？", description: "先看低轨星座、政企通信、遥感和直连终端是否形成持续订单。" },
  网络与系统: { title: "运载与卫星系统", question: "怎样把卫星低成本送入轨道？", description: "运载火箭、可重复使用和整星系统共同决定部署速度与单位成本。" },
  光互联产品: { title: "整星、终端与服务", question: "收入最后落到哪里？", description: "区分整星批产、地面终端、卫星运营和应用服务，不把一次发射等同于利润。" },
  核心器件: { title: "关键分系统", question: "哪些器件决定可靠性和产能？", description: "发动机、电推进、相控阵、星载芯片、连接器与复合材料是主要技术和供给约束。" },
  制造与配套: { title: "制造、发射与测控", question: "怎样批量制造并稳定发射？", description: "批产良率、商业发射场、测发测控、回收设施与频轨许可共同决定兑现节奏。" }
};

function isPCBIndustry(name: string) {
  return name.toUpperCase().includes("PCB");
}

function isOpticalIndustry(name: string) {
  return name.includes("光通信") || name.includes("光互联") || name.includes("光模块");
}

function isCloudIndustry(name: string) {
  return name.includes("云计算") || name.includes("算力租赁") || name.includes("AI云");
}

function isDomesticComputeIndustry(name: string) {
  return name.includes("国产算力");
}

function isCommercialSpaceIndustry(name: string) {
  return name.includes("商业航天") || name.includes("卫星互联网") || name.includes("可重复使用火箭");
}

function nodeTypeMeta(industryName: string, type: NodeType) {
  if (isCommercialSpaceIndustry(industryName)) return SPACE_NODE_TYPE_META[type];
  if (isDomesticComputeIndustry(industryName)) return DOMESTIC_COMPUTE_NODE_TYPE_META[type];
  if (isPCBIndustry(industryName)) return PCB_NODE_TYPE_META[type];
  if (isCloudIndustry(industryName)) return CLOUD_NODE_TYPE_META[type];
  return NODE_TYPE_META[type];
}
const COMPANY_MARKETS: CompanyMarket[] = ["A股", "美股", "台湾", "韩国", "日本", "其他"];
const TRACKING_STATUSES: TrackingStatus[] = ["核心受益", "重点跟踪", "观察", "淘汰"];
const VERIFICATION_STATUSES: VerificationStatus[] = ["未验证", "验证中", "已确认", "失败"];
const CATALYST_STATUSES: CatalystStatus[] = ["预期", "确认", "兑现"];
const CATALYST_TYPES = ["新品发布", "资本开支", "订单", "价格", "政策", "业绩", "认证", "产能", "其他"];
const DRIVERS = ["需求", "供给", "价格", "资本开支", "技术变化", "政策", "国产替代"];
const RESEARCH_SOURCE_PREFERENCES = ["公告/财报", "公司官网", "政府/监管", "行业组织", "市场行情", "雪球线索"];
const CAUSAL_STAGES: CausalStage[] = ["真实变化", "认知扩散", "资金进入", "筹码交换", "拥挤退潮"];
const CAUSAL_EVIDENCE_TYPES: CausalEvidenceType[] = ["硬事实", "市场线索", "盘面确认", "市场推断"];
const CAUSAL_SIGNAL_STATUSES: CausalSignalStatus[] = ["线索", "已确认", "减弱", "失效"];
const SELL_PRESSURES = ["低", "中", "高"] as const;

function formatTime(value?: string | null) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value.slice(0, 16) : parsed.toLocaleString("zh-CN", { hour12: false });
}

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "等待中",
  running: "进行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消"
};

function generationErrorText(value?: string | null) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  const lower = text.toLowerCase();
  if (lower.includes("invalid_json_schema") || lower.includes("invalid schema for response_format") || lower.includes("additionalproperties")) {
    return "生成规则校验失败，系统已修正，请重新发起全面更新。";
  }
  if (lower.includes("invalid_request_error")) return "Codex请求格式校验失败，请重新发起全面更新。";
  if (!text) return "Codex生成失败，请稍后重试。";
  return text.length > 180 ? `${text.slice(0, 180)}…` : text;
}

function formatPercent(value?: number | null) {
  if (value === null || value === undefined) return "-";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function formatPlainPercent(value?: number | null) {
  if (value === null || value === undefined) return "-";
  return `${value.toFixed(2)}%`;
}

function formatAmount(value?: number | null) {
  if (value === null || value === undefined) return "-";
  return `${(value / 100_000_000).toFixed(1)}亿`;
}

function formatMarketCap(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return "-";
  return `${(value / 100_000_000).toFixed(value >= 100_000_000_000 ? 0 : 1)}亿元`;
}

function formatConsensusAmount(value?: number | null, currency = "USD") {
  if (value == null || !Number.isFinite(value)) return "-";
  const labels: Record<string, string> = { CNY: "元", USD: "美元", HKD: "港元", TWD: "新台币", JPY: "日元", KRW: "韩元", GBP: "英镑", SEK: "瑞典克朗" };
  return `${(value / 100_000_000).toFixed(value >= 100_000_000_000 ? 0 : 1)}亿${labels[currency] || currency}`;
}

function formatConsensusEps(value?: number | null, currency = "USD") {
  if (value == null || !Number.isFinite(value)) return "-";
  const symbols: Record<string, string> = { CNY: "¥", USD: "$", HKD: "HK$", TWD: "NT$", JPY: "¥", KRW: "₩", GBP: "£", SEK: "kr " };
  return `${symbols[currency] || `${currency} `}${value.toFixed(2)}`;
}

function formatMultiple(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return "-";
  return `${value.toFixed(2)}x`;
}

type ValuationSupportDriver = NonNullable<CompanyExpectationAnalysis["valuation"]["support_drivers"]>[number];

function supportDriverColor(status: string) {
  if (["支撑", "有缓冲"].includes(status)) return "green";
  if (["转弱", "失效"].includes(status)) return "red";
  if (["重点验证", "高要求", "分歧", "观察"].includes(status)) return "gold";
  return "blue";
}

function formatSupportDriverValue(driver: ValuationSupportDriver) {
  if (driver.value_kind === "currency") return formatConsensusAmount(driver.value, driver.currency || "USD");
  if (driver.value_kind === "eps") return formatConsensusEps(driver.value, driver.currency || "USD");
  if (driver.value_kind === "percent") return formatPercent(driver.value);
  return formatMultiple(driver.value);
}

function supportDriverSecondary(driver: ValuationSupportDriver) {
  if (driver.key === "revenue_scale") return `同比 ${formatPercent(driver.change_pct)} · ${driver.coverage_count}家机构`;
  if (driver.key === "profit_quality") return `隐含净利率 ${formatPlainPercent(driver.change_pct)} · ${driver.coverage_count}家机构`;
  if (driver.key === "consensus_revision") return `90日 ${formatPercent(driver.change_pct)} · 30日修订样本${driver.coverage_count}家`;
  return `远期PS ${formatMultiple(driver.change_pct)} · 历史样本${driver.coverage_count}期`;
}

function expectationGapColor(value: ExpectationGapStatus) {
  if (value === "正向预期差") return "green";
  if (value === "负向预期差") return "red";
  if (value === "基本匹配") return "gold";
  return undefined;
}

function crossMarketTagColor(value: string) {
  if (["增强", "有效", "跨市场共振", "基本同步"].includes(value)) return "green";
  if (["转弱", "失效", "风险收缩"].includes(value)) return "red";
  if (["分化", "减弱", "偏离观察", "检查独立催化", "A股落后", "A股领先"].includes(value)) return "gold";
  return undefined;
}

function crossMarketReturnClass(value?: number | null) {
  if (value === null || value === undefined) return "is-flat";
  if (value > 0.15) return "is-positive";
  if (value < -0.15) return "is-negative";
  return "is-flat";
}

type CrossMarketDetailKind = "global" | "overseas" | "a_share" | "relation" | "divergence";
type CrossMarketDetailSelection = { sector: IndustryCrossMarketSector; kind: CrossMarketDetailKind };

const CROSS_MARKET_DETAIL_LABELS: Record<CrossMarketDetailKind, string> = {
  global: "全球需求",
  overseas: "海外直接链",
  a_share: "A股表达",
  relation: "60日关系",
  divergence: "偏离与下一步"
};

function basketDetailCopy(status: IndustryCrossMarketBasket["status"], scope: "global" | "overseas" | "a_share") {
  if (scope === "global") {
    if (status === "增强") return "全球AI需求与科技风险偏好正在增强，但这里只能确认外部环境改善，不能直接推出A股会涨。";
    if (status === "转弱") return "全球需求代理近期转弱，应继续核对云厂商资本开支、服务器订单和官方指引是否同步降温。";
    if (status === "分化") return "需求端与网络端走势不一致，当前还不能形成清晰的全球需求方向。";
    return "可用样本不足，暂时不能据此判断全球需求方向。";
  }
  if (scope === "overseas") {
    if (status === "增强") return "海外同产业公司的价格正在共同走强，产业预期有直接表达；仍要用订单、ASP和利润率验证。";
    if (status === "转弱") return "海外直接产业链共同转弱，说明产业预期正在降温或前期交易正在兑现。";
    if (status === "分化") return "海外同链公司表现分化，可能是产品结构、客户或业绩节奏不同，需要逐家公司排查。";
    return "海外直接代理不足，不能把单只股票波动当成产业结论。";
  }
  if (status === "增强") return "A股产业链已出现较广泛的自身表达，下一步检查成交额、龙头强度和基本面证据能否持续。";
  if (status === "转弱") return "A股产业链整体转弱，新增买盘不足，当前不宜只依据海外上涨推断补涨。";
  if (status === "分化") return "A股内部强弱分化，应找出真正有订单与利润兑现的公司，避免用板块平均掩盖差异。";
  return "A股有效样本不足，暂时不能判断产业链是否形成共同表达。";
}

function CrossMarketDetailModal({ selection, data, onClose, onEnterResearch }: {
  selection: CrossMarketDetailSelection | null;
  data: IndustryCrossMarketIntelligence;
  onClose: () => void;
  onEnterResearch?: (id: number) => void;
}) {
  if (!selection) return null;
  const { sector, kind } = selection;
  const basket = kind === "global" ? sector.global_demand : kind === "overseas" ? sector.overseas_direct : kind === "a_share" ? sector.a_share : null;
  const proxies: IndustryCrossMarketProxy[] = kind === "global"
    ? [...sector.proxies.demand, ...sector.proxies.network]
    : kind === "overseas"
      ? sector.proxies.direct
      : kind === "a_share"
        ? sector.proxies.a_share
        : [];
  const availableProxies = [...proxies].filter((item) => item.available).sort((left, right) => (right.return_5d_pct ?? -999) - (left.return_5d_pct ?? -999));
  const connectedNodeMappings = sector.node_mappings.filter((item) => item.overseas.length && item.a_share.length);
  const detailLabel = CROSS_MARKET_DETAIL_LABELS[kind];
  const interpretation = basket
    ? basketDetailCopy(basket.status, kind as "global" | "overseas" | "a_share")
    : kind === "relation"
      ? sector.relation_state === "有效"
        ? "过去60个对齐交易日中，海外直接链与A股方向具有可用关系，可以观察偏离，但仍不是机械回归规律。"
        : sector.relation_state === "减弱"
          ? "海外与A股的同步关系正在减弱，偏离只能作为线索，不能直接当作补涨或回落信号。"
          : "当前关系失效或样本不足，海外涨跌不能用来推断A股下一步。"
      : sector.action_reason;
  const conclusion = basket?.status || (kind === "relation" ? sector.relation_state : `${sector.divergence_state} · ${sector.action}`);
  const positiveProxyCount = availableProxies.filter((item) => (item.return_5d_pct ?? 0) > 0).length;
  const strongestProxy = availableProxies[0];
  const weakestProxy = availableProxies[availableProxies.length - 1];
  const globalBreadthText = !basket?.sample_count
    ? "样本不足"
    : positiveProxyCount === 0
      ? "全面转弱"
      : positiveProxyCount === basket.sample_count
        ? "全面走强"
        : positiveProxyCount / basket.sample_count >= 0.6
          ? "多数走强"
          : positiveProxyCount / basket.sample_count <= 0.4
            ? "多数走弱"
            : "内部有分化";
  const globalAShareMeaning = sector.global_demand.status === "增强"
    ? "海外需求温度与科技风险偏好改善，A股相关方向获得更好的外部环境；但只有A股成交额、龙头强度和订单业绩同步改善，才能升级为可交易信号。"
    : sector.global_demand.status === "转弱"
      ? "海外需求代理与科技风险偏好承压，会降低A股相关方向的估值容忍度；但这不等于A股基本面已经转弱，更不能仅凭海外下跌判断A股必然补跌。"
      : sector.global_demand.status === "分化"
        ? "海外公司走势不一致，暂时没有统一的外部方向。A股应回到自身订单、业绩和盘面强度，不能用单一海外公司替代产业判断。"
        : "当前海外样本不足，暂不参与A股方向判断，只保留为待补数据。";
  const nextSteps = kind === "global"
    ? ["核对海外云厂商资本开支与AI网络预算", "确认服务器、交换机和光互联订单是否同步变化", "优先采用财报、公告和公司指引，不用股价替代产业证据"]
    : kind === "overseas"
      ? ["逐家公司核对订单、收入、ASP与毛利率", "分清共同行业变化和单家公司独立催化", "检查海外强弱是否传导到相同产业节点"]
      : kind === "a_share"
        ? ["检查板块成交额、上涨家数和龙头持续性", "确认A股公司订单、客户认证、收入和利润兑现", "只保留产业表达直接、基本面可验证的公司"]
        : kind === "relation"
          ? ["相关性高于0.30才视为可用，0.10至0.30只作弱提示", "观察Beta是否稳定，不把一次异常波动当成结构关系", "关系减弱或失效时停止使用偏离回归判断"]
          : ["先等A股自身出现成交额与价格表达", "再核对订单、价格、收入或利润等硬证据", "若海外与A股继续共同转弱，优先防守而不是等待补涨"];

  return <Modal
    centered
    width={980}
    open
    className="cross-market-detail-modal"
    onCancel={onClose}
    title={<div className="cross-market-detail-title"><span>跨市场价格验证</span><strong>{sector.name.replace(/（.*$/, "")} · {detailLabel}</strong><small>点击数据看结论，不把相关性或偏离直接当成买点</small></div>}
    footer={<Space><Button onClick={onClose}>关闭</Button>{onEnterResearch ? <Button type="primary" onClick={() => { onClose(); onEnterResearch(sector.id); }}>进入产业研究 <ArrowRightOutlined /></Button> : null}</Space>}
  >
    <div className={`cross-market-detail-body tone-${sector.action_tone}`}>
      {kind === "global" && basket ? <>
        <section className="cross-market-global-hero">
          <div className="cross-market-global-conclusion">
            <span>全球需求温度</span>
            <strong>{conclusion}</strong>
            <p>{interpretation}</p>
          </div>
          <div className="cross-market-global-context">
            <Tag color={crossMarketTagColor(conclusion)}>{globalBreadthText}</Tag>
            <b>{positiveProxyCount}/{basket.sample_count} 上涨</b>
            <small>{basket.trade_date || "日期待补"} · {basket.sample_count}个价格代理</small>
          </div>
        </section>

        <div className="cross-market-global-trend" aria-label="全球需求不同时间窗口表现">
          <div><span>短线 · 1日</span><strong className={crossMarketReturnClass(basket.return_1d_pct)}>{formatPercent(basket.return_1d_pct)}</strong><small>最新交易日</small></div>
          <div className="is-primary"><span>主判断 · 5日</span><strong className={crossMarketReturnClass(basket.return_5d_pct)}>{formatPercent(basket.return_5d_pct)}</strong><small>观察方向变化</small></div>
          <div><span>中期 · 20日</span><strong className={crossMarketReturnClass(basket.return_20d_pct)}>{formatPercent(basket.return_20d_pct)}</strong><small>观察趋势延续</small></div>
          <div><span>内部广度 · 5日</span><strong>{positiveProxyCount}/{basket.sample_count}</strong><small>{globalBreadthText}</small></div>
        </div>

        {availableProxies.length ? <section className="cross-market-global-structure">
          <header><div><h4>强弱由谁造成</h4><p>先看共同方向，再找支撑与拖累，避免被单只大公司误导。</p></div></header>
          <div>
            <article>
              <span>{(strongestProxy?.return_5d_pct ?? 0) > 0 ? "主要支撑" : "相对抗跌"}</span>
              <strong>{strongestProxy?.name || "-"}</strong>
              <b className={crossMarketReturnClass(strongestProxy?.return_5d_pct)}>{formatPercent(strongestProxy?.return_5d_pct)}</b>
              <small>{strongestProxy?.role || "价格代理"} · 5日</small>
            </article>
            <article>
              <span>主要拖累</span>
              <strong>{weakestProxy?.name || "-"}</strong>
              <b className={crossMarketReturnClass(weakestProxy?.return_5d_pct)}>{formatPercent(weakestProxy?.return_5d_pct)}</b>
              <small>{weakestProxy?.role || "价格代理"} · 5日</small>
            </article>
            <article>
              <span>一致程度</span>
              <strong>{globalBreadthText}</strong>
              <b>{positiveProxyCount}/{basket.sample_count}</b>
              <small>5日上涨样本</small>
            </article>
          </div>
        </section> : null}

        <div className="cross-market-global-actions">
          <section>
            <span>传导到A股怎么理解</span>
            <h4>这是外部环境信号，不是A股买卖结论</h4>
            <p>{globalAShareMeaning}</p>
          </section>
          <section>
            <span>下一步只核三件事</span>
            <ol>{nextSteps.map((item) => <li key={item}>{item}</li>)}</ol>
          </section>
        </div>
      </> : <>
        <section className="cross-market-detail-hero">
          <div><span>当前结论</span><strong>{conclusion}</strong><p>{interpretation}</p></div>
          <Tag color={crossMarketTagColor(kind === "divergence" ? sector.action : conclusion)}>{kind === "divergence" ? sector.action : detailLabel}</Tag>
        </section>

        <div className="cross-market-detail-metrics">
          {basket ? <>
            <div><span>1日变化</span><strong>{formatPercent(basket.return_1d_pct)}</strong></div>
            <div><span>5日变化</span><strong>{formatPercent(basket.return_5d_pct)}</strong></div>
            <div><span>20日变化</span><strong>{formatPercent(basket.return_20d_pct)}</strong></div>
            <div><span>5日上涨占比</span><strong>{formatPercent(basket.breadth_5d_pct)}</strong><small>{basket.sample_count}个样本 · {basket.trade_date || "日期待补"}</small></div>
          </> : <>
            <div><span>60日相关</span><strong>{sector.correlation_60d ?? "-"}</strong></div>
            <div><span>Beta</span><strong>{sector.beta_60d ?? "-"}</strong><small>海外每变化1%，A股历史平均响应幅度</small></div>
            <div><span>20日残差</span><strong>{formatPercent(sector.residual_20d_pct)}</strong></div>
            <div><span>偏离Z值</span><strong>{sector.divergence_z ?? "-"}</strong><small>{sector.aligned_samples}组对齐样本</small></div>
          </>}
        </div>

        <div className="cross-market-detail-columns">
          <section>
            <h4>这意味着什么</h4>
            <p>{interpretation}</p>
            {kind === "relation" ? <div className="cross-market-relation-guide"><span><b>≥ 0.30</b>关系可用</span><span><b>0.10–0.30</b>关系减弱</span><span><b>&lt; 0.10</b>关系失效</span></div> : null}
            {kind === "divergence" ? <Alert type={sector.action_tone === "negative" ? "error" : "warning"} showIcon message={sector.action} description={sector.action_reason} /> : null}
          </section>
          <section>
            <h4>下一步怎么验证</h4>
            <ol>{nextSteps.map((item) => <li key={item}>{item}</li>)}</ol>
          </section>
        </div>
      </>}

      {availableProxies.length ? <section className="cross-market-detail-section">
        <div className="cross-market-detail-section-title"><h4>{kind === "global" ? "全部价格代理" : "价格代理构成"}</h4><span>{availableProxies.length}个有效样本，按5日表现排序</span></div>
        <div className="cross-market-detail-proxies">
          {availableProxies.map((row) => <div key={`${row.role}-${row.symbol}`}>
            <span><strong>{row.name}<Tag>{row.role}</Tag></strong><small>{row.symbol} · {row.market} · {row.trade_date || "日期待补"}</small></span>
            <b className={crossMarketReturnClass(row.return_1d_pct)}>{formatPercent(row.return_1d_pct)}<small>1日</small></b>
            <b className={crossMarketReturnClass(row.return_5d_pct)}>{formatPercent(row.return_5d_pct)}<small>5日</small></b>
            <b className={crossMarketReturnClass(row.return_20d_pct)}>{formatPercent(row.return_20d_pct)}<small>20日</small></b>
          </div>)}
        </div>
      </section> : null}

      {(kind === "overseas" || kind === "a_share" || kind === "divergence") && connectedNodeMappings.length ? <section className="cross-market-detail-section">
        <div className="cross-market-detail-section-title"><h4>产业节点传导</h4><span>{connectedNodeMappings.length}个同环节映射，只展示海外与A股两端都已连接的节点</span></div>
        <div className="cross-market-detail-node-map">
          {connectedNodeMappings.map((item) => <div key={item.node_id}>
            <span><small>{item.node_type}</small><strong>{item.node_name}</strong></span>
            <p>{item.overseas.length ? item.overseas.map((row) => row.name).join("、") : "海外代理待补"}</p>
            <i>→</i>
            <p>{item.a_share.length ? item.a_share.map((row) => row.name).join("、") : "A股映射待补"}</p>
          </div>)}
        </div>
      </section> : null}

      {kind === "global" ? <details className="cross-market-detail-boundary is-collapsible"><summary>查看口径与使用边界</summary><span>这里观察的是海外上市公司价格形成的“需求温度”，不是订单或收入本身。跨市场价格只负责确认盘面，不能替代产业证据；最终仍要由资本开支、订单、公司指引与A股自身表达共同确认。</span></details> : <div className="cross-market-detail-boundary"><strong>使用边界</strong><span>跨市场价格只负责确认盘面。偏离表示“值得检查”，不等于补涨买点；最终仍要由产业证据与A股自身表达共同确认。</span></div>}
      <div className="cross-market-detail-source">{data.method.alignment} · {data.method.source} · 数据截至 {data.as_of || "-"}</div>
    </div>
  </Modal>;
}

function CrossMarketOverview({ data, loading, refreshing, onRefresh, onSelect }: {
  data?: IndustryCrossMarketIntelligence;
  loading: boolean;
  refreshing: boolean;
  onRefresh: () => void;
  onSelect: (id: number) => void;
}) {
  const [selectedDetail, setSelectedDetail] = useState<CrossMarketDetailSelection | null>(null);
  if (loading && !data) return <section className="cross-market-overview is-loading">正在对齐海外产业链与A股历史行情...</section>;
  if (!data || !data.sectors.length) return <Alert type="warning" showIcon message="跨市场价格验证暂不可用" description={data?.errors[0] || "等待海外行情返回"} />;
  return <section className="cross-market-overview">
    <header>
      <div><span>跨市场价格验证层</span><strong>海外产业链 → A股表达 → 偏离状态</strong><small>{data.principle}</small></div>
      <Space><Tag color={data.source_status === "ok" ? "green" : "gold"}>{data.source_status === "ok" ? "数据完整" : "部分数据"}</Tag><Button size="small" icon={<ReloadOutlined />} loading={refreshing} onClick={onRefresh}>刷新行情</Button></Space>
    </header>
    <div className="cross-market-overview-head" aria-hidden="true"><span>细分方向 / 提示</span><span>全球需求</span><span>海外直接链</span><span>A股表达</span><span>60日关系</span><span>偏离 / 下一步</span></div>
    <div className="cross-market-overview-rows">
      {data.sectors.map((sector) => <article key={sector.id} className={`cross-market-overview-row tone-${sector.action_tone}`}>
        <button type="button" className="cross-market-sector-name" onClick={() => onSelect(sector.id)} aria-label={`进入${sector.name}产业研究`}><small>细分方向</small><strong>{sector.name.replace(/（.*$/, "")}</strong><Tag color={crossMarketTagColor(sector.action)}>{sector.action}</Tag><i>进入研究 <ArrowRightOutlined /></i></button>
        <button type="button" className="cross-market-metric-cell" onClick={() => setSelectedDetail({ sector, kind: "global" })} aria-label={`查看${sector.name}全球需求详情`}><small>全球需求</small><b>{sector.global_demand.status}</b><em>5日 {formatPercent(sector.global_demand.return_5d_pct)} · 20日 {formatPercent(sector.global_demand.return_20d_pct)}</em><i>查看详情 <ArrowRightOutlined /></i></button>
        <button type="button" className="cross-market-metric-cell" onClick={() => setSelectedDetail({ sector, kind: "overseas" })} aria-label={`查看${sector.name}海外直接链详情`}><small>海外直接链</small><b>{sector.overseas_direct.status}</b><em>{sector.overseas_direct.sample_count}个代理 · 5日 {formatPercent(sector.overseas_direct.return_5d_pct)}</em><i>查看详情 <ArrowRightOutlined /></i></button>
        <button type="button" className="cross-market-metric-cell" onClick={() => setSelectedDetail({ sector, kind: "a_share" })} aria-label={`查看${sector.name}A股表达详情`}><small>A股表达</small><b>{sector.a_share.status}</b><em>{sector.a_share.sample_count}家公司 · 5日 {formatPercent(sector.a_share.return_5d_pct)}</em><i>查看详情 <ArrowRightOutlined /></i></button>
        <button type="button" className="cross-market-metric-cell" onClick={() => setSelectedDetail({ sector, kind: "relation" })} aria-label={`查看${sector.name}60日关系详情`}><small>60日关系</small><b>{sector.relation_state}</b><em>相关 {sector.correlation_60d ?? "-"} · {sector.aligned_samples}组</em><i>查看详情 <ArrowRightOutlined /></i></button>
        <button type="button" className="cross-market-metric-cell cross-market-next" onClick={() => setSelectedDetail({ sector, kind: "divergence" })} aria-label={`查看${sector.name}偏离与下一步详情`}><small>偏离 / 下一步</small><b>{sector.divergence_state}</b><em>{sector.action_reason}</em><i>查看详细结论 <ArrowRightOutlined /></i></button>
      </article>)}
    </div>
    <footer><span>数据截至 {data.as_of || "-"}</span><span>{data.method.alignment}</span><span>5/20日判断趋势，60日只检查关系是否仍有效</span></footer>
    <CrossMarketDetailModal selection={selectedDetail} data={data} onClose={() => setSelectedDetail(null)} onEnterResearch={onSelect} />
  </section>;
}

function CrossMarketSectorPanel({ sector, data, refreshing, onRefresh }: {
  sector?: IndustryCrossMarketSector;
  data?: IndustryCrossMarketIntelligence;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const [selectedDetail, setSelectedDetail] = useState<CrossMarketDetailSelection | null>(null);
  if (!sector || !data) return <Alert type="warning" showIcon message="跨市场验证数据尚未就绪" description="不会影响产业正式结论，稍后可手工刷新。" />;
  const stages = [
    { kind: "global" as const, label: "全球需求", value: sector.global_demand.status, metric: `5日 ${formatPercent(sector.global_demand.return_5d_pct)}`, note: "只判断AI需求和风险温度" },
    { kind: "overseas" as const, label: "海外直接链", value: sector.overseas_direct.status, metric: `20日 ${formatPercent(sector.overseas_direct.return_20d_pct)}`, note: `${sector.overseas_direct.sample_count}个直接产业代理` },
    { kind: "a_share" as const, label: "A股自身表达", value: sector.a_share.status, metric: `5日 ${formatPercent(sector.a_share.return_5d_pct)}`, note: `${sector.a_share.sample_count}家公司等权观察` },
    { kind: "relation" as const, label: "60日关系", value: sector.relation_state, metric: `相关 ${sector.correlation_60d ?? "-"}`, note: `${sector.aligned_samples}组对齐样本` },
    { kind: "divergence" as const, label: "偏离与提示", value: sector.divergence_state, metric: sector.action, note: sector.action_reason }
  ];
  const proxyGroups = [
    { title: "需求温度", rows: sector.proxies.demand },
    { title: "网络传导", rows: sector.proxies.network },
    { title: "海外直接链", rows: sector.proxies.direct },
    { title: "A股表达", rows: sector.proxies.a_share }
  ];
  return <section className="cross-market-sector-panel">
    <header><div><Typography.Title level={4}>跨市场验证</Typography.Title><Typography.Text>先看海外同链是否共同表达，再看A股是否出现自身表达；偏离不是补涨信号。</Typography.Text></div><Button size="small" icon={<ReloadOutlined />} loading={refreshing} onClick={onRefresh}>刷新</Button></header>
    <div className="cross-market-stage-flow">
      {stages.map((stage, index) => <button type="button" key={stage.label} className={index === stages.length - 1 ? `tone-${sector.action_tone}` : ""} onClick={() => setSelectedDetail({ sector, kind: stage.kind })}><span>{index + 1} · {stage.label}</span><strong>{stage.value}</strong><b>{stage.metric}</b><p>{stage.note}</p><i>查看详细结论 <ArrowRightOutlined /></i></button>)}
    </div>
    <details className="cross-market-details">
      <summary><span>查看代理构成与产业节点映射</span><small>{sector.proxies.direct.filter((item) => item.available).length}个海外直接代理有行情 · {sector.node_mappings.length}个产业节点</small></summary>
      <div className="cross-market-proxy-groups">
        {proxyGroups.map((group) => <section key={group.title}><strong>{group.title}</strong><div>{group.rows.length ? group.rows.map((row) => <span key={`${group.title}-${row.symbol}`} className={row.available ? "" : "is-unavailable"}><b>{row.name}</b><small>{row.symbol} · 5日 {formatPercent(row.return_5d_pct)}</small></span>) : <em>暂无映射</em>}</div></section>)}
      </div>
      <div className="cross-market-node-map">
        {sector.node_mappings.filter((item) => item.overseas.length || item.a_share.length).map((item) => <article key={item.node_id}><div><small>{item.node_type}</small><strong>{item.node_name}</strong></div><span>{item.overseas.length ? item.overseas.map((row) => row.name).join("、") : "海外代理待补"}</span><i>→</i><span>{item.a_share.length ? item.a_share.map((row) => row.name).join("、") : "A股映射待补"}</span></article>)}
      </div>
    </details>
    <footer><Tag>盘面确认</Tag><span>{data.principle}</span><small>{data.method.source} · 数据 {data.as_of || "-"}</small></footer>
    <CrossMarketDetailModal selection={selectedDetail} data={data} onClose={() => setSelectedDetail(null)} />
  </section>;
}

function verdictColor(value: InvestmentVerdict) {
  if (value === "通过") return "green";
  if (value === "否决") return "red";
  return "gold";
}

function pricingColor(value: PricingStatus) {
  if (value === "未定价") return "green";
  if (value === "充分定价") return "red";
  return "gold";
}

function expectationPricingLabel(value: PricingStatus) {
  if (value === "未定价") return "尚未反映";
  if (value === "充分定价") return "充分反映";
  return "部分反映";
}

function expectationGrowthLabel(value: string) {
  if (value === "高度同向增长") return "高增长";
  if (value === "多数同向增长") return "多数增长";
  if (value === "预期分化") return "增长分化";
  if (value === "多数转弱") return "增长转弱";
  return "预期待补";
}

function expectationRevisionLabel(value: string) {
  return value.replace("盈利预期同步", "").replace("盈利预期", "").replace("修订样本积累中", "修订待积累");
}

function catalystStatusColor(value: CatalystStatus) {
  if (value === "兑现") return "green";
  if (value === "确认") return "blue";
  return "gold";
}

function TrendStars({ strength }: { strength: number }) {
  const count = Math.max(1, Math.min(5, Math.ceil(strength / 20)));
  return <span className="trend-stars" aria-label={`趋势强度${count}星`}>{Array.from({ length: 5 }, (_, index) => <StarFilled key={index} className={index < count ? "is-on" : ""} />)}</span>;
}

function VerdictCard({ label, value, note }: { label: string; value: InvestmentVerdict; note?: string | null }) {
  return (
    <div className="trend-verdict-card">
      <Typography.Text>{label}</Typography.Text>
      <strong className={`verdict-${value}`}>{value}</strong>
      <small>{note || "等待补充判断依据"}</small>
    </div>
  );
}

function companyRepresentativeSummary(items: IndustryCompanyRecommendation[], limit = 3) {
  if (!items.length) return "待标注";
  const names = items.slice(0, limit).map((item) => item.name).join("、");
  return items.length > limit ? `${names}等${items.length}家` : names;
}

function companyRepresentativeDetail(items: IndustryCompanyRecommendation[]) {
  if (!items.length) return "待标注";
  return items.map((item) => `${item.position || "环节待标注"}：${item.name}`).join("；");
}

function AIIndustryIntelligence({ data, crossMarket, loading, crossMarketLoading, crossMarketRefreshing, onRefreshCrossMarket, onSelect }: {
  data?: IndustryIntelligence;
  crossMarket?: IndustryCrossMarketIntelligence;
  loading: boolean;
  crossMarketLoading: boolean;
  crossMarketRefreshing: boolean;
  onRefreshCrossMarket: () => void;
  onSelect: (id: number) => void;
}) {
  const [selectedNews, setSelectedNews] = useState<IndustryIntelligence["fresh_changes"][number] | null>(null);

  if (loading || !data) {
    return <section className="ai-intelligence-panel-v2 is-loading">正在汇总新变化、细分传导与盘面状态...</section>;
  }

  const summary = data.summary;
  const verificationLabel = (value: string) => ({ verified: "已验证", cross_verified: "交叉验证", partial: "部分验证", unverified: "待验证" }[value] || value || "待验证");
  const priceLabel = (value: string) => ({ untraded: "尚未交易", first_expression: "首次表达", multi_rounds: "已交易多轮", negative: "负反馈", no_confirmation: "无盘面确认", unknown: "定价待判断" }[value] || value || "定价待判断");
  const officialCheckLabel = (value: string) => ({ matched: "官方公告命中", not_found: "官方已查未命中", error: "官方查询失败", missing_stock: "缺股票代码", not_required: "无需公告核验" }[value] || value || "待核验");
  const shortName = (value: string) => value.replace(/（.*$/, "");

  return <>
    <section className="ai-intelligence-panel-v2">
      <div className="ai-intelligence-compact-meta">
        <span>{data.theme}共同情报</span>
        <small>更新至 {formatTime(data.overall.latest_at)}</small>
      </div>

      <div className={`ai-intelligence-decision is-${data.overall.tone}`}>
        <div><span>当前动作</span><strong>{data.overall.action}</strong><p>{data.overall.reason}</p></div>
        <div className="ai-intelligence-live-stats">
          <span><b>{summary.fresh_changes_7d}</b>7日变化</span>
          <span><b>{summary.verified_changes_7d}</b>硬证据</span>
          <span><b>{summary.shared_changes_7d}</b>共同变量</span>
          <span><b>{summary.confirmed_validations}/{summary.total_validations}</b>已经兑现</span>
        </div>
      </div>

      <details className="cross-market-overview-disclosure">
        <summary><strong>跨市场价格验证</strong><span>需要时展开；不替代产业证据</span></summary>
        <CrossMarketOverview data={crossMarket} loading={crossMarketLoading} refreshing={crossMarketRefreshing} onRefresh={onRefreshCrossMarket} onSelect={onSelect} />
      </details>

      <div className="ai-intelligence-bottom-grid">
        <section className="ai-intelligence-fresh-changes">
          <div className="ai-intelligence-section-title"><div><strong>今日增量</strong><span>信息投研自动关联产业、节点和公司；硬证据直接计入，线索进入观察层</span></div><Tag color="blue">{data.fresh_changes.length} 条</Tag></div>
          <div className="ai-fresh-change-list">
            {data.fresh_changes.length ? data.fresh_changes.slice(0, 8).map((item, index) => (
              <button type="button" key={item.item_key} className={`ai-news-card ${item.verification_status === "cross_verified" || item.verification_status === "verified" ? "is-verified" : "is-pending"}`} onClick={() => setSelectedNews(item)}>
                <div className="ai-news-card-head">
                  <span className="ai-fresh-change-index">{index + 1}</span>
                  <time>{formatTime(item.published_at)}</time>
                  <Tag color={item.auto_tier === "硬证据" ? "green" : item.auto_tier === "部分验证" ? "blue" : "gold"}>{item.auto_tier}</Tag>
                </div>
                <strong title={item.title}>{item.title}</strong>
                <p>{item.source_name || "来源待确认"}</p>
                <div className="ai-news-card-auto-path"><span>自动关联</span><em>{item.affected_nodes.slice(0, 2).map((node) => node.name).join("、") || item.affected_companies.slice(0, 2).map((company) => company.name).join("、") || item.affected_industries.map((industry) => shortName(industry.name)).join("、")}</em></div>
                <div className="ai-news-card-footer">
                  <Space size={4} wrap>{item.affected_industries.map((industry) => <Tag key={industry.id}>{shortName(industry.name)}</Tag>)}</Space>
                  <span>{item.is_shared ? "共同变量 · " : ""}{priceLabel(item.price_status)} <ArrowRightOutlined /></span>
                </div>
              </button>
            )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今天的信息投研中暂无可自动关联的新变化" />}
          </div>
        </section>

        <section className="ai-intelligence-verification">
          <div className="ai-intelligence-section-title"><div><strong>兑现进度</strong><span>消息只有沿产业链落到利润，才算完成验证</span></div><Tag color={summary.risk_evidence ? "red" : "green"}>{summary.risk_evidence ? `${summary.risk_evidence} 项反向` : "暂无反向"}</Tag></div>
          <div className="ai-verification-rail">
            {data.validation_route.map((stage, index) => (
              <div key={stage.stage} className={stage.failed ? "has-failed" : stage.confirmed === stage.total && stage.total ? "is-confirmed" : ""}>
                <span>{index + 1}</span><strong>{stage.stage}</strong><em>{stage.confirmed}/{stage.total}</em>{stage.failed ? <small>{stage.failed} 失败</small> : <small>{stage.in_progress} 验证中</small>}
              </div>
            ))}
          </div>
          <div className="ai-intelligence-risk-summary">
            <span>最大风险</span><p>{data.risk_signals[0] ? `${data.risk_signals[0].signal_type || "风险"}：${data.risk_signals[0].title}` : data.sectors.find((item) => item.action_tone === "negative")?.action_reason || "暂无已记录反证，仍需主动检查风险"}</p>
          </div>
        </section>
      </div>

      <section className="ai-intelligence-transmission">
        <div className="ai-intelligence-section-title"><div><strong>细分方向比较</strong><span>同一口径横向比较，先选方向，再进入详细研究</span></div><Tag>{summary.industries} 个方向</Tag></div>
        <div className="ai-sector-compare-table">
          <div className="ai-sector-compare-head" aria-hidden="true">
            <span>方向 / 动作</span><span>产业证据</span><span>信息扩散</span><span>盘面确认</span><span>预期与定价</span><span>全球优势代表</span><span>国内替代</span><span>下一触发</span>
          </div>
          {data.sectors.map((sector) => (
            <button key={sector.id} type="button" className={`ai-sector-compare-row is-${sector.action_tone}`} onClick={() => onSelect(sector.id)}>
              <span className="ai-sector-compare-name"><small>方向 / 动作</small><strong>{shortName(sector.name)}</strong><em>{sector.phase} · {sector.attention_level}</em><i>{sector.action}</i></span>
              <span><small>产业证据</small><b>{sector.evidence_status}</b><em>{sector.validation_summary.confirmed}/{sector.validation_summary.total} 项确认</em></span>
              <span title={`近7日多源信息 ${sector.attention.sample_7d} 条，覆盖 ${sector.attention.source_count_7d} 个来源、${sector.attention.channel_count_7d} 类渠道；雪球仅 ${sector.attention.xq_7d} 条、${sector.attention.xq_author_count_7d} 位作者`}><small>信息扩散</small><b>{sector.attention.status}</b><em>7日 {sector.attention.sample_7d} 条 · {sector.attention.source_count_7d} 来源</em></span>
              <span><small>盘面确认</small><b>{sector.market.status}</b><em>5日上涨覆盖 {sector.market.breadth_5d_pct ?? "-"}%</em></span>
              <span className={`ai-sector-consensus pricing-${sector.pricing_status}`} title={`${sector.sector_expectation.summary} 当前定价状态：${expectationPricingLabel(sector.pricing_status)}。尚无统一的实际业绩相对一致预期偏差时，不判断超预期或低于预期。`}>
                <small>预期与定价</small><b>{expectationPricingLabel(sector.pricing_status)}</b>
                <em>{expectationGrowthLabel(sector.sector_expectation.growth_status)} · {expectationRevisionLabel(sector.sector_expectation.revision_status)}</em>
                <i title={`兑现结果待核：超预期 / 符合预期 / 低于预期；机构覆盖 ${sector.sector_expectation.consensus_count}/${sector.sector_expectation.listed_count} 家`}>兑现待核</i>
              </span>
              <span title={`全球优势代表（按环节）：${companyRepresentativeDetail(sector.company_recommendations.global_leaders)}`}><small>全球优势代表（按环节）</small><b>{companyRepresentativeSummary(sector.company_recommendations.global_leaders)}</b><em>{sector.company_recommendations.global_leaders.length ? `覆盖 ${sector.company_recommendations.global_leaders.length} 家，进入研究看全量` : "代表而非唯一"}</em></span>
              <span><small>国内替代</small><b>{sector.company_recommendations.domestic_alternatives[0]?.name || "待标注"}</b></span>
              <span className="ai-sector-compare-next"><small>下一触发</small><b title={sector.next_signal || "待补充"}>{sector.next_signal || "待补充"}</b><em>进入研究 <ArrowRightOutlined /></em></span>
            </button>
          ))}
        </div>
      </section>

      <details className="ai-intelligence-history">
        <summary>查看历史证据摘要（{summary.sources} 条去重材料）</summary>
        <div>
          <section><strong>正面硬证据</strong>{data.supporting_evidence.slice(0, 5).map((item) => <p key={item.key || item.title}>{item.title}<span>{item.affected_industries.map((industry) => shortName(industry.name)).join("、")}</span></p>)}</section>
          <section><strong>反向证据与证伪</strong>{data.risk_signals.slice(0, 5).map((item) => <p key={`${item.signal_type}-${item.title}`}>{item.title}<span>{item.affected_industries.map((industry) => shortName(industry.name)).join("、")}</span></p>)}</section>
        </div>
      </details>
    </section>

    <Modal
      open={Boolean(selectedNews)}
      title="信息增量详情"
      centered
      width={780}
      className="ai-news-detail-modal"
      onCancel={() => setSelectedNews(null)}
      footer={selectedNews ? <Space wrap>
        <Button onClick={() => setSelectedNews(null)}>关闭</Button>
        {selectedNews.affected_industries.map((industry) => <Button key={industry.id} onClick={() => { setSelectedNews(null); onSelect(industry.id); }}>进入{shortName(industry.name)}研究</Button>)}
        {selectedNews.source_url ? <Button type="primary" icon={<LinkOutlined />} href={selectedNews.source_url} target="_blank" rel="noreferrer">查看原始来源</Button> : null}
      </Space> : null}
    >
      {selectedNews ? <div className="ai-news-detail-content">
        <div className="ai-news-detail-tags">
          <Tag color={selectedNews.auto_tier === "硬证据" ? "green" : selectedNews.auto_tier === "部分验证" ? "blue" : "gold"}>自动·{selectedNews.auto_tier}</Tag>
          <Tag color={selectedNews.verification_status === "cross_verified" || selectedNews.verification_status === "verified" ? "green" : "gold"}>{verificationLabel(selectedNews.verification_status)}</Tag>
          <Tag>{priceLabel(selectedNews.price_status)}</Tag>
          {selectedNews.is_shared ? <Tag color="cyan">跨细分共同变量</Tag> : null}
          <Tag color={selectedNews.importance >= 5 ? "red" : selectedNews.importance >= 4 ? "orange" : "default"}>重要度 {selectedNews.importance}</Tag>
        </div>
        <Typography.Title level={4}>{selectedNews.title}</Typography.Title>
        <div className="ai-news-detail-summary"><span>内容摘要</span><p>{selectedNews.summary || "暂无摘要，请查看原始来源。"}</p></div>
        <section className="ai-news-auto-association">
          <header><div><span>自动吸收结果</span><strong>{selectedNews.auto_action}</strong></div><Tag color="cyan">无需人工转入</Tag></header>
          <div>
            <span>产业</span><p>{selectedNews.affected_industries.length ? selectedNews.affected_industries.map((industry) => <Tag key={industry.id}>{shortName(industry.name)} · {industry.match_score}</Tag>) : "未关联"}</p>
            <span>节点</span><p>{selectedNews.affected_nodes.length ? selectedNews.affected_nodes.map((node) => <Tag key={node.id}>{node.name}</Tag>) : "由产业级信息自动承接"}</p>
            <span>公司</span><p>{selectedNews.affected_companies.length ? selectedNews.affected_companies.map((company) => <Tag key={company.id}>{company.name}{company.code ? ` ${company.code}` : ""}</Tag>) : "未指向单一公司"}</p>
          </div>
          <footer>{selectedNews.match_reasons.join("；") || "按产业关键词自动匹配"}</footer>
        </section>
        <div className="ai-news-detail-meta">
          <div><span>发布时间</span><strong>{formatTime(selectedNews.published_at)}</strong></div>
          <div><span>信息来源</span><strong>{selectedNews.source_name || "来源待确认"}</strong></div>
          <div><span>官方核验</span><strong>{officialCheckLabel(selectedNews.official_check_status)}</strong></div>
          <div><span>关联方向</span><strong>{selectedNews.affected_industries.map((industry) => shortName(industry.name)).join("、") || "待关联"}</strong></div>
        </div>
      </div> : null}
    </Modal>
  </>;
}

function LogicView({ detail }: { detail: IndustryTrendDetail }) {
  return (
    <div className="trend-logic-grid">
      <div><span>产业变化</span><p>{detail.change_summary || "未填写"}</p></div>
      <div><span>为什么现在</span><p>{detail.why_now || "未填写"}</p></div>
      <div><span>核心逻辑</span><p>{detail.investment_logic || "未填写"}</p></div>
      <div><span>预期周期</span><p>{detail.expected_duration || "未填写"}</p></div>
      <div><span>已被定价</span><p>{detail.priced_in || "未填写"}</p></div>
      <div><span>尚未定价</span><p>{detail.not_priced_in || "未填写"}</p></div>
      <div className="is-wide"><span>核心驱动</span><Space wrap>{detail.drivers.length ? detail.drivers.map((item) => <Tag color="cyan" key={item}>{item}</Tag>) : <Typography.Text type="secondary">未填写</Typography.Text>}</Space></div>
      <div className="is-wide"><span>主要风险</span><p>{detail.risk || "未填写"}</p></div>
    </div>
  );
}

function draftString(payload: Record<string, unknown>, key: string, fallback = "未提供") {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value : fallback;
}

function draftStrings(payload: Record<string, unknown>, key: string) {
  const value = payload[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
}

function draftRecords(payload: Record<string, unknown>, key: string) {
  const value = payload[key];
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
}

function DraftReview({ draft }: { draft: IndustryTrendDraft }) {
  const payload = draft.payload;
  const companies = draftRecords(payload, "companies");
  const catalysts = draftRecords(payload, "catalysts");
  const validations = draftRecords(payload, "validations");
  const sources = draftRecords(payload, "sources");
  const invalidated = draftStrings(payload, "invalidated_information");
  const validationChanges = draftStrings(payload, "validation_changes");
  const verdictItems = [
    ["方向", draftString(payload, "direction_verdict")],
    ["股票", draftString(payload, "stock_verdict")],
    ["时点", draftString(payload, "timing_verdict")],
    ["市场定价", draftString(payload, "pricing_status")]
  ];
  const updateItems = [
    ["阶段变化", draftString(payload, "phase_change_reason", "无调整建议")],
    ["股票排序", draftString(payload, "stock_ranking_change", "无调整建议")],
    ["A股交易首选", draftString(payload, "primary_company_assessment", "无调整建议")],
    ["定价变化", draftString(payload, "pricing_change", "无调整建议")],
    ["判断变化", draftString(payload, "decision_change_reason", "无调整建议")]
  ];
  if (payload.delta_version === "1") {
    const coverage = draftRecords(payload, "coverage");
    const nodeChanges = draftRecords(payload, "nodes");
    const companyChanges = draftRecords(payload, "companies");
    const validationChanges = draftRecords(payload, "validations");
    const catalystChanges = draftRecords(payload, "catalysts");
    const newSources = draftRecords(payload, "new_sources");
    const contradictions = draftStrings(payload, "contradictions");
    const invalidatedInformation = draftStrings(payload, "invalidated_information");
    const corePatch = payload.core_patch && typeof payload.core_patch === "object" ? payload.core_patch as Record<string, unknown> : {};
    const structureChanges: Record<string, unknown>[] = [
      ...nodeChanges.map((item) => ({ type: "节点", ...item })),
      ...companyChanges.map((item) => ({ type: "公司", ...item })),
      ...validationChanges.map((item) => ({ type: "验证", ...item })),
      ...catalystChanges.map((item) => ({ type: "事件", ...item }))
    ];
    return (
      <div className="trend-draft-review trend-delta-review">
        <div className="trend-draft-summary"><span>全面更新结论</span><strong>{draftString(payload, "update_summary")}</strong></div>
        <div className="trend-delta-stats">
          <div><span>节点变化</span><strong>{nodeChanges.length}</strong></div>
          <div><span>公司变化</span><strong>{companyChanges.length}</strong></div>
          <div><span>验证变化</span><strong>{validationChanges.length}</strong></div>
          <div><span>新增来源</span><strong>{newSources.length}</strong></div>
        </div>
        <section className="trend-draft-section">
          <Typography.Title level={5}>覆盖检查</Typography.Title>
          <div className="trend-coverage-grid">{coverage.map((item, index) => <div key={`${String(item.area)}-${index}`}><span>{String(item.area || "未命名维度")}</span><Tag color={item.status === "已更新" ? "green" : item.status === "存在冲突" ? "red" : item.status === "证据不足" ? "gold" : undefined}>{String(item.status || "无变化")}</Tag><p>{String(item.finding || item.gap || "未发现有效变化")}</p></div>)}</div>
        </section>
        {Object.keys(corePatch).length ? <section className="trend-draft-section"><Typography.Title level={5}>正式判断建议</Typography.Title><pre className="trend-draft-json">{JSON.stringify(corePatch, null, 2)}</pre></section> : null}
        {contradictions.length || invalidatedInformation.length ? <section className="trend-draft-section"><Typography.Title level={5}>冲突与失效</Typography.Title>{contradictions.map((item) => <Alert key={item} type="error" showIcon message={item} />)}{invalidatedInformation.map((item) => <Alert key={item} type="warning" showIcon message={item} />)}</section> : null}
        {structureChanges.length ? <section className="trend-draft-section"><Typography.Title level={5}>本次结构变化</Typography.Title><List size="small" dataSource={structureChanges} renderItem={(item) => <List.Item><Tag>{String(item.type)}</Tag><Tag color={item.action === "新增" ? "green" : "blue"}>{String(item.action || "更新")}</Tag><strong>{String(item.name || item.event_name || "未命名")}</strong></List.Item>} /></section> : null}
        <section className="trend-draft-section"><Typography.Title level={5}>新增证据</Typography.Title>{newSources.length ? newSources.map((item, index) => <div className="trend-draft-source" key={`${String(item.source_url || item.title)}-${index}`}><span>{String(item.evidence_date || "-")} · {String(item.source_name || "未注明来源")}</span>{item.source_url ? <a href={String(item.source_url)} target="_blank" rel="noreferrer">{String(item.title || "未命名来源")}</a> : <strong>{String(item.title || "未命名来源")}</strong>}</div>) : <Typography.Text type="secondary">全面检查后没有新增有效证据</Typography.Text>}</section>
      </div>
    );
  }
  return (
    <div className="trend-draft-review">
      <div className="trend-draft-summary">
        <span>{draft.draft_type === "update" ? "本次变化" : "一句话判断"}</span>
        <strong>{draft.draft_type === "update" ? draftString(payload, "update_summary", draftString(payload, "summary")) : draftString(payload, "summary")}</strong>
      </div>
      <div className="trend-draft-verdicts">
        {verdictItems.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
      </div>
      <div className="trend-draft-logic">
        <div><span>产业变化</span><p>{draftString(payload, "change_summary")}</p></div>
        <div><span>为什么现在</span><p>{draftString(payload, "why_now")}</p></div>
        <div><span>下一确认信号</span><p>{draftString(payload, "next_signal")}</p></div>
        <div><span>证伪条件</span><p>{draftString(payload, "invalidation")}</p></div>
      </div>
      {draft.draft_type === "update" ? (
        <section className="trend-draft-section">
          <Typography.Title level={5}>本次调整建议</Typography.Title>
          {updateItems.map(([label, value]) => <div className="trend-draft-change" key={label}><span>{label}</span><p>{value}</p></div>)}
          {invalidated.length ? <div className="trend-draft-change"><span>已失效信息</span><ul>{invalidated.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
          {validationChanges.length ? <div className="trend-draft-change"><span>验证项变化</span><ul>{validationChanges.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
        </section>
      ) : null}
      <section className="trend-draft-section">
        <Typography.Title level={5}>候选股票</Typography.Title>
        {companies.length ? companies.map((item, index) => (
          <div className="trend-draft-company" key={`${String(item.code || "")}-${index}`}>
            <strong>{String(item.name || "未命名")} <small>{String(item.code || "")}</small></strong>
            <Space size={4} wrap>{item.is_global_leader ? <Tag color="purple">全球绝对优势</Tag> : null}{item.is_domestic_alternative ? <Tag color="cyan">国内可替代</Tag> : null}<Tag color={item.is_primary ? "gold" : undefined}>{item.is_primary ? "A股交易首选" : String(item.tracking_status || "观察")}</Tag></Space>
            <p>{String(item.primary_reason || item.core_advantage || "未提供比较依据")}</p>
          </div>
        )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="草稿未提供合格A股表达" />}
      </section>
      <section className="trend-draft-section">
        <Typography.Title level={5}>关键事件催化</Typography.Title>
        {catalysts.length ? catalysts.map((item, index) => <div className="trend-draft-change" key={`${String(item.event_name || "")}-${index}`}><span>{String(item.expected_time || "待定")}</span><p><strong>{String(item.event_name || "未命名事件")}</strong><br />{String(item.impact || "未提供影响")}</p></div>) : <Typography.Text type="secondary">草稿未提供关键催化事件</Typography.Text>}
      </section>
      <section className="trend-draft-section">
        <Typography.Title level={5}>投资验证路线图</Typography.Title>
        {validations.length ? validations.map((item, index) => <div className="trend-draft-change" key={`${String(item.name || "")}-${index}`}><span>验证 {index + 1}</span><p><strong>{String(item.name || "未命名验证")}</strong><br />{String(item.criteria || "未提供确认标准")}</p></div>) : <Typography.Text type="secondary">草稿未提供验证路线</Typography.Text>}
      </section>
      <section className="trend-draft-section">
        <Typography.Title level={5}>核心来源</Typography.Title>
        {sources.length ? sources.map((item, index) => {
          const title = String(item.title || "未命名来源");
          const url = typeof item.source_url === "string" ? item.source_url : "";
          return <div className="trend-draft-source" key={`${url}-${index}`}><span>{String(item.evidence_date || "-")} · {String(item.source_name || "未注明来源")}</span>{url ? <a href={url} target="_blank" rel="noreferrer">{title}</a> : <strong>{title}</strong>}</div>;
        }) : <Typography.Text type="secondary">草稿未提供可打开的来源</Typography.Text>}
      </section>
      <details className="trend-draft-raw"><summary>查看结构化原始数据</summary><pre className="trend-draft-json">{JSON.stringify(payload, null, 2)}</pre></details>
    </div>
  );
}

function IndustryExpectationPanel({
  data,
  loading,
  refreshing,
  onRefresh,
  onCompany
}: {
  data?: IndustryExpectationSummary;
  loading: boolean;
  refreshing: boolean;
  onRefresh: () => void;
  onCompany: (companyId: number) => void;
}) {
  if (loading && !data) return <section className="industry-expectation-panel"><div className="industry-expectation-loading">正在汇总全部产业公司的机构一致预期…</div></section>;
  if (!data) return <Alert type="warning" showIcon message="板块一致预期暂未就绪" />;
  const groups = data.groups.filter((group) => ["all", "global", "domestic"].includes(group.key));
  const calibration = data.expectation_gap_calibration;
  const aShareMethod = calibration?.markets?.["A股"];
  const usMethod = calibration?.markets?.["美股"];
  const aThresholds = aShareMethod?.thresholds || {};
  const aValidation = aShareMethod?.validation;
  const statusColor = (status: IndustryExpectationCompany["coverage_status"]) => status === "已覆盖" ? "green" : status === "无分析师覆盖" ? "gold" : status === "未上市" ? "default" : "blue";
  const gateColor = (code: string) => code === "A" ? "green" : code === "B" ? "gold" : code === "C" ? "blue" : code === "D" ? "red" : "default";
  const columns: ColumnsType<IndustryExpectationCompany> = [
    { title: "公司", width: 190, render: (_, row) => <button type="button" className="industry-expectation-company-link" onClick={() => onCompany(row.company_id)}><strong>{row.name}</strong><small>{row.market} · {row.code || "-"}</small></button> },
    { title: "五年校准", width: 185, render: (_, row) => row.expectation_gap_gate ? <div className="industry-expectation-gate-cell"><Tag color={gateColor(row.expectation_gap_gate.decision_code)}>{row.expectation_gap_gate.label}</Tag><small>{row.expectation_gap_gate.reason}</small></div> : "-" },
    { title: "覆盖", width: 105, render: (_, row) => <Tag color={statusColor(row.coverage_status)}>{row.coverage_status}</Tag> },
    { title: "下一年营收", width: 115, render: (_, row) => <span className={(row.next_revenue_growth_pct ?? 0) >= 0 ? "market-up" : "market-down"}>{formatPercent(row.next_revenue_growth_pct)}</span> },
    { title: "下一年EPS", width: 110, render: (_, row) => <span className={(row.next_eps_growth_pct ?? 0) >= 0 ? "market-up" : "market-down"}>{formatPercent(row.next_eps_growth_pct)}</span> },
    { title: "30日修订", width: 105, render: (_, row) => <span className={(row.eps_revision_30d_pct ?? 0) >= 0 ? "market-up" : "market-down"}>{formatPercent(row.eps_revision_30d_pct)}</span> },
    { title: "远期PS", width: 90, render: (_, row) => formatMultiple(row.forward_ps) },
    { title: "远期PE", width: 90, render: (_, row) => formatMultiple(row.forward_pe) },
    { title: "预测期", width: 105, render: (_, row) => row.forecast_period?.slice(0, 4) || "-" },
    { title: "来源", width: 170, render: (_, row) => row.provider || "-" }
  ];
  return (
    <section className="industry-expectation-panel">
      <header>
        <div><Typography.Title level={4}>板块预期与一致性</Typography.Title><Typography.Text>先看机构预测是否同向，再回到订单、价格和盘面验证</Typography.Text></div>
        <Button icon={<ReloadOutlined />} loading={refreshing} onClick={onRefresh}>补齐并更新</Button>
      </header>
      <section className="industry-expectation-calibration">
        <header>
          <div><strong>预期差五年校准门</strong><span>{calibration?.period?.start || "-"} 至 {calibration?.period?.end || "-"} · {calibration?.method_version || "校准待就绪"}</span></div>
          <Space wrap size={5}><Tag color={aShareMethod?.state === "validated" ? "green" : "gold"}>A股 {aShareMethod?.state === "validated" ? "留出样本通过" : "未验证"}</Tag><Tag color="red">美股 {usMethod?.state === "validated" ? "已验证" : "未验证·仅观察"}</Tag></Space>
        </header>
        <div className="industry-expectation-calibration-results">
          <article><span>A类留出样本</span><strong>{aValidation?.a_sample_count ?? "-"}</strong><small>独立验证期，不含阈值选择样本</small></article>
          <article><span>20日超额中位</span><strong>{formatPercent(aValidation?.a_median_excess_return_pct)}</strong><small>95% CI {formatPercent(aValidation?.a_ci_low_pct)} 至 {formatPercent(aValidation?.a_ci_high_pct)}</small></article>
          <article><span>A类胜率</span><strong>{formatPlainPercent(aValidation?.a_win_rate_pct)}</strong><small>A 相对 B 中位增益 {formatPercent(aValidation?.pricing_gain_a_vs_b_median_pct)}</small></article>
          <article><span>当前使用边界</span><strong>A股严格校准</strong><small>美股失败样本保留，不自动升级</small></article>
        </div>
        <div className="industry-expectation-calibration-flow">
          <article><b>1</b><span>一致预期方向</span><small>板块层只看同向与修订</small></article><ArrowRightOutlined />
          <article><b>2</b><span>公司硬证据</span><small>增速 ≥ {formatPlainPercent(aThresholds.evidence_growth_pct)} · 加速度 ≥ {formatPlainPercent(aThresholds.evidence_acceleration_pct)}</small></article><ArrowRightOutlined />
          <article><b>3</b><span>披露前定价</span><small>前20日相对沪深300 ≤ {formatPlainPercent(aThresholds.unpriced_20d_excess_pct)}</small></article><ArrowRightOutlined />
          <article><b>4</b><span>首日盘面确认</span><small>首个可交易日超额 ≥ {formatPlainPercent(aThresholds.confirmation_excess_pct)}</small></article><ArrowRightOutlined />
          <article className="is-result"><b>5</b><span>A / B / C / D</span><small>只给校准建议，不覆盖正式结论</small></article>
        </div>
        <div className="industry-expectation-calibration-decisions">
          {(["A", "B", "C", "D"] as const).map((code) => <div key={code}><Tag color={gateColor(code)}>{code}</Tag><span>{calibration?.decision_rules?.[code] || "规则待加载"}</span></div>)}
        </div>
      </section>
      <div className="industry-expectation-statusbar">
        <article><span>股票全集</span><strong>{data.universe.unique_stocks}</strong><small>{data.universe.unlisted_companies}家未上市另列</small></article>
        <article><span>一致预期覆盖</span><strong>{data.universe.consensus_companies}/{data.universe.listed_companies}</strong><small>{formatPlainPercent(data.universe.coverage_rate)}</small></article>
        <article><span>增长一致性</span><strong>{data.consistency.growth_status}</strong><small>营收与EPS同时增长</small></article>
        <article><span>预期修订</span><strong>{data.consistency.revision_status}</strong><small>只统计有30日历史的市场</small></article>
        <article><span>海外 → 国内</span><strong>{data.consistency.cross_group_status}</strong><small>全球优势对照国内替代</small></article>
      </div>
      <div className="industry-expectation-groups">
        {groups.map((group) => <article key={group.key}>
          <header><strong>{group.label}</strong><span>{group.consensus_count}/{group.company_count}覆盖</span></header>
          <div><span>同向增长</span><b>{formatPlainPercent(group.positive_growth_ratio)}</b></div>
          <div><span>营收增速中位</span><b>{formatPercent(group.median_revenue_growth_pct)}</b></div>
          <div><span>EPS增速中位</span><b>{formatPercent(group.median_eps_growth_pct)}</b></div>
          <div><span>估值中位</span><b>PS {formatMultiple(group.median_forward_ps)} · PE {formatMultiple(group.median_forward_pe)}</b></div>
          <small>30日修订样本 {group.revision_sample_count}家{group.revision_sample_count ? ` · 上修占比 ${formatPlainPercent(group.revision_up_ratio)}` : " · 正在积累"}</small>
        </article>)}
      </div>
      <div className="industry-expectation-conclusion"><Tag color={data.consistency.growth_status.includes("增长") ? "green" : "gold"}>{data.consistency.growth_status}</Tag><strong>{data.consistency.conclusion}</strong></div>
      <details className="industry-expectation-universe">
        <summary>查看全部公司预期与缺口 <span>{data.universe.no_analyst_coverage}家无分析师覆盖 · {data.universe.pending_fetch}家待抓取</span></summary>
        <Table rowKey="company_id" size="small" scroll={{ x: 1280 }} columns={columns} dataSource={data.companies} pagination={{ pageSize: 12, hideOnSinglePage: true }} />
      </details>
      <footer>{data.method_note} · 数据快照 {data.snapshot_date}</footer>
    </section>
  );
}

function CompanyDetailModal({
  company,
  detail,
  calibrationGate,
  methodCalibration,
  onClose,
  onOpenStock,
  onEditCompany
}: {
  company: IndustryTrendCompany | null;
  detail: IndustryTrendDetail | null;
  calibrationGate: CompanyExpectationGapGate | null;
  methodCalibration: ExpectationGapCalibration | null;
  onClose: () => void;
  onOpenStock: (company: IndustryTrendCompany) => void;
  onEditCompany: (company: IndustryTrendCompany) => void;
}) {
  const queryClient = useQueryClient();
  const expectationQuery = useQuery({
    queryKey: ["company-expectation-analysis", company?.chain_id, company?.id],
    queryFn: () => industryTrendApi.companyExpectation(company!.chain_id, company!.id),
    enabled: Boolean(company && ["A股", "美股"].includes(company.market) && company.code),
    staleTime: 6 * 60 * 60 * 1000,
    retry: false
  });
  const expectationRefresh = useMutation({
    mutationFn: () => industryTrendApi.refreshCompanyExpectation(company!.chain_id, company!.id),
    onSuccess: (data: CompanyExpectationAnalysis) => {
      queryClient.setQueryData(["company-expectation-analysis", company?.chain_id, company?.id], data);
    }
  });
  if (!company || !detail) return null;
  const nodes = detail.nodes.filter((node) => company.node_ids.includes(node.id));
  const validations = detail.validations.filter((item) => item.company_id === company.id || (item.node_id ? company.node_ids.includes(item.node_id) : false));
  const catalysts = detail.catalysts.filter((item) => item.impact_company_id === company.id || (item.impact_node_id ? company.node_ids.includes(item.impact_node_id) : false));
  const sources = detail.sources.filter((source) => source.company_id === company.id || source.node_ids.some((nodeId) => company.node_ids.includes(nodeId)));
  const directSources = sources.filter((source) => source.company_id === company.id);
  const directHardSources = directSources.filter((source) => ["官方硬证据", "公司披露", "行业标准"].includes(source.source_tier) && source.evidence_state === "有效");
  const directConfirmedValidations = validations.filter((item) => item.company_id === company.id && item.status === "已确认");
  const market = company.market_snapshot;
  const marketCap = company.market_cap_snapshot;
  const autoExpectation = expectationQuery.data;
  const nextConsensus = autoExpectation?.consensus.next_year;
  const reverseCheck = autoExpectation?.valuation.reverse_check;
  const marketCapBridge = autoExpectation?.valuation.market_cap_bridge;
  const supportDrivers = autoExpectation?.valuation.support_drivers || [];
  const companyValuationFactors = validations
    .filter((item) => item.company_id === company.id)
    .sort((left, right) => left.sort_order - right.sort_order);
  const marketPricingReady = Boolean(autoExpectation?.valuation.market_cap && (nextConsensus?.revenue.average || nextConsensus?.eps.average));
  const displayMarketCap = marketCap?.total_market_cap ?? autoExpectation?.valuation.market_cap ?? null;
  const displayMarketCapText = marketCap?.total_market_cap != null
    ? formatMarketCap(marketCap.total_market_cap)
    : formatConsensusAmount(autoExpectation?.valuation.market_cap, autoExpectation?.consensus.currency);
  const expectationComplete = Boolean(company.market_implied_expectation && company.evidence_based_expectation && company.expectation_gap_reason);
  const gapStatus: ExpectationGapStatus = expectationComplete ? company.expectation_gap_status : "无法判断";
  const gapLabel = gapStatus === "无法判断" && marketPricingReady ? "市场已反推 · 证据待验证" : gapStatus;
  const evidenceReady = company.verification_status === "已确认" && (directHardSources.length > 0 || directConfirmedValidations.length > 0);
  const capMoveSinceAnchor = displayMarketCap && company.expectation_anchor_market_cap
    ? (displayMarketCap / company.expectation_anchor_market_cap - 1) * 100
    : null;
  const expectationAge = company.expectation_as_of
    ? Math.floor((Date.now() - new Date(`${company.expectation_as_of}T00:00:00`).getTime()) / 86_400_000)
    : null;
  const needsReview = Boolean((expectationAge != null && expectationAge > 30) || (capMoveSinceAnchor != null && Math.abs(capMoveSinceAnchor) >= 15));
  const gapAction = gapStatus === "正向预期差"
    ? !evidenceReady
      ? "只有预期线索，等待公司级硬证据"
      : company.pricing_status === "充分定价" || needsReview
        ? "价格或时间已变化，需要重新测算"
        : !company.expectation_trigger
          ? "方向可能成立，但缺少可执行触发器"
          : "保留正向预期差，等待触发验证"
    : gapStatus === "负向预期差"
      ? "市值要求高于证据，优先防守和检查证伪"
    : gapStatus === "基本匹配"
        ? "逻辑可能成立，但当前赔率一般"
        : marketPricingReady
          ? "市场定价已反推；等待把公司指引、订单和价格换算成可比的营收利润"
          : "缺少市值隐含预期或证据锚，暂不判断赔率";
  const statusColor = company.verification_status === "已确认" ? "green" : company.verification_status === "失败" ? "red" : "gold";
  const automaticMarketExpectation = marketPricingReady
    ? `${nextConsensus?.end_date || "下一财年"}一致预期营收 ${formatConsensusAmount(nextConsensus?.revenue.average, autoExpectation?.consensus.currency)}、EPS ${formatConsensusEps(nextConsensus?.eps.average, autoExpectation?.consensus.currency)}；当前市值对应 ${formatMultiple(autoExpectation?.valuation.forward_ps)} PS、${formatMultiple(autoExpectation?.valuation.forward_pe)} PE。`
    : null;
  const directEvidenceSummary = directHardSources.length
    ? directHardSources.slice(0, 2).map((source) => source.title).join("；")
    : null;

  return (
    <Modal
      open
      centered
      rootClassName="industry-company-detail-modal"
      width={900}
      title={(
        <div className="industry-company-detail-title">
          <div><strong>{company.name}</strong><span>{company.exchange || company.market} · {company.code || "未披露代码"}</span></div>
          <Space wrap size={5}>
            <Tag color={company.market === "A股" ? "red" : company.market === "美股" ? "blue" : "cyan"}>{company.market}</Tag>
            <Tag color={statusColor}>{company.verification_status}</Tag>
            {company.is_global_leader ? <Tag color="purple">全球绝对优势</Tag> : null}
            {company.is_domestic_alternative ? <Tag color="cyan">国内可替代</Tag> : null}
            {company.is_primary ? <Tag color="orange">A股交易首选</Tag> : null}
          </Space>
        </div>
      )}
      onCancel={onClose}
      footer={<Space wrap><Button onClick={onClose}>关闭</Button><Button onClick={() => onEditCompany(company)}>编辑预期差</Button>{company.external_url ? <Button href={company.external_url} target="_blank">官网 / 投资者关系</Button> : null}{company.market === "A股" && company.full_code ? <Button type="primary" onClick={() => onOpenStock(company)}>进入个股投研</Button> : null}</Space>}
    >
      <div className="industry-company-detail-view">
        <section className="industry-company-hero">
          <div>
            <span>一句话定位</span>
            <strong>{company.company_standing || company.position || "产业位置待补充"}</strong>
            <p>{company.core_advantage || "核心优势仍待补充和验证。"}</p>
          </div>
          <div className="industry-company-status-grid">
            <div><span>关注状态</span><strong>{company.tracking_status}</strong></div>
            <div><span>市场定价</span><strong>{company.pricing_status}</strong></div>
            <div><span>受益直接性</span><strong>{company.benefit_directness || "待判断"}</strong></div>
          </div>
        </section>

        <section className={`industry-company-section industry-company-expectation tone-${gapStatus}`}>
          <header><div><strong>市值与预期差</strong><span>不是判断公司好不好，而是比较“当前市值需要相信什么”和“硬证据实际支持什么”</span></div><Tag color={expectationGapColor(gapStatus)}>{gapLabel}</Tag></header>
          <div className="industry-company-expectation-metrics">
            <div><span>当前总市值</span><strong>{displayMarketCapText}</strong><small>{marketCap?.as_of || autoExpectation?.valuation.as_of || "日期待补"} · {marketCap?.is_estimated ? "按收盘价推算" : marketCap?.method || autoExpectation?.valuation.quote_source || "暂无市值"}</small></div>
            <div><span>判断时市值锚</span><strong>{formatMarketCap(company.expectation_anchor_market_cap)}</strong><small>{company.expectation_as_of || "尚未冻结判断日"}</small></div>
            <div><span>市值变化</span><strong>{capMoveSinceAnchor == null ? "-" : formatPercent(capMoveSinceAnchor)}</strong><small>{capMoveSinceAnchor == null ? "建立市值锚后才能跟踪消化程度" : "相对预期差判断时"}</small></div>
            <div><span>证据门</span><strong>{evidenceReady ? "公司级已确认" : "仍待公司级确认"}</strong><small>{directHardSources.length} 条直接硬来源 · {directConfirmedValidations.length} 项直接验证</small></div>
          </div>
          {["A股", "美股"].includes(company.market) ? (
            <div className="industry-company-consensus">
              <div className="industry-company-consensus-heading">
                <div><strong>机构一致预期 × 当前市值反推</strong><span>{autoExpectation ? `${autoExpectation.provider} · ${autoExpectation.snapshot_date}快照` : "一致预期与历史定价校验分开保存"}</span></div>
                <Button size="small" icon={<ReloadOutlined />} loading={expectationRefresh.isPending} onClick={() => expectationRefresh.mutate()}>更新预期</Button>
              </div>
              {expectationQuery.isLoading ? <div className="industry-company-consensus-loading">正在获取分析师一致预期、SEC财务与历史价格…</div> : null}
              {expectationQuery.isError ? <Alert type="warning" showIcon message="一致预期暂未取得" description={String(expectationQuery.error)} /> : null}
              {autoExpectation ? <>
                {autoExpectation.message ? <Alert type="warning" showIcon message={autoExpectation.message} /> : null}
                <div className="industry-company-consensus-grid">
                  <article><span>{nextConsensus?.end_date || "下一财年"}营收一致预期</span><strong>{formatConsensusAmount(nextConsensus?.revenue.average, autoExpectation.consensus.currency)}</strong><small>{nextConsensus?.revenue.analyst_count || 0}位分析师{nextConsensus?.revenue.low != null ? ` · 区间 ${formatConsensusAmount(nextConsensus.revenue.low, autoExpectation.consensus.currency)}–${formatConsensusAmount(nextConsensus.revenue.high, autoExpectation.consensus.currency)}` : ""}</small></article>
                  <article><span>下一财年EPS一致预期</span><strong>{formatConsensusEps(nextConsensus?.eps.average, autoExpectation.consensus.currency)}</strong><small>{nextConsensus?.eps.analyst_count || 0}位分析师 · 30日修订 {formatPercent(nextConsensus?.eps_revision.change_30d_pct)}</small></article>
                  <article><span>当前市值 / 下一财年营收</span><strong>{formatMultiple(autoExpectation.valuation.forward_ps)}</strong><small>当前市值 {formatConsensusAmount(autoExpectation.valuation.market_cap, autoExpectation.consensus.currency)}</small></article>
                  <article><span>当前股价 / 下一财年EPS</span><strong>{formatMultiple(autoExpectation.valuation.forward_pe)}</strong><small>一致预期净利率 {formatPlainPercent(autoExpectation.valuation.consensus_net_margin_pct)}</small></article>
                </div>
                <div className="industry-company-pricing-readout">
                  <Tag color={autoExpectation.valuation.pricing_state.includes("高增长") ? "gold" : "blue"}>{autoExpectation.valuation.pricing_state}</Tag>
                  <strong>{autoExpectation.valuation.conclusion}</strong>
                </div>
                {autoExpectation.backtest.sample_start ? <><div className="industry-company-reverse-grid">
                  <article><span>按历史中位PS反推</span><strong>需营收 {formatConsensusAmount(reverseCheck?.revenue_required_at_historical_median_ps, autoExpectation.consensus.currency)}</strong><small>比当前一致预期 {formatPercent(reverseCheck?.consensus_revenue_gap_vs_median_ps_pct)}</small></article>
                  <article><span>按历史中位PE反推</span><strong>需EPS {formatConsensusEps(reverseCheck?.eps_required_at_historical_median_pe, autoExpectation.consensus.currency)}</strong><small>比当前一致预期 {formatPercent(reverseCheck?.consensus_eps_gap_vs_median_pe_pct)}</small></article>
                  <article><span>历史回测样本</span><strong>{autoExpectation.backtest.sample_start}–{autoExpectation.backtest.sample_end}</strong><small>远期PS中位 {formatMultiple(autoExpectation.backtest.forward_ps.median)} · 盈利年份PE中位 {formatMultiple(autoExpectation.backtest.profitable_forward_pe.median)}</small></article>
                </div>
                <details className="industry-company-backtest-details">
                  <summary>查看历史定价区间与数据来源</summary>
                  <p>{autoExpectation.backtest.method}</p>
                  <div><span>远期PS</span><b>{formatMultiple(autoExpectation.backtest.forward_ps.min)} – {formatMultiple(autoExpectation.backtest.forward_ps.max)}</b><small>25/50/75分位：{formatMultiple(autoExpectation.backtest.forward_ps.p25)} / {formatMultiple(autoExpectation.backtest.forward_ps.median)} / {formatMultiple(autoExpectation.backtest.forward_ps.p75)}</small></div>
                  <div><span>盈利年份远期PE</span><b>{formatMultiple(autoExpectation.backtest.profitable_forward_pe.min)} – {formatMultiple(autoExpectation.backtest.profitable_forward_pe.max)}</b><small>25/50/75分位：{formatMultiple(autoExpectation.backtest.profitable_forward_pe.p25)} / {formatMultiple(autoExpectation.backtest.profitable_forward_pe.median)} / {formatMultiple(autoExpectation.backtest.profitable_forward_pe.p75)}</small></div>
                  <footer>{autoExpectation.sources.filter((source) => source.url).map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.name} · {source.tier}</a>)}</footer>
                </details></> : <div className="industry-company-consensus-archive"><strong>历史一致预期开始留档</strong><span>{autoExpectation.backtest.method}</span>{autoExpectation.sources.filter((source) => source.url).map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">查看 {source.name}</a>)}</div>}
              </> : null}
            </div>
          ) : null}
          {(marketCapBridge?.market_cap || supportDrivers.length || companyValuationFactors.length) ? (
            <div className="industry-company-driver-monitor">
              <div className="industry-company-driver-heading">
                <div><strong>远期市值支撑因子</strong><span>把市值拆成可跟踪的假设；看哪个因子增强、转弱或失效</span></div>
                <Tag color="blue">不使用综合分</Tag>
              </div>
              {marketCapBridge?.market_cap ? <>
                <div className="industry-company-market-cap-bridge">
                  <article><span>{marketCapBridge.forecast_period || "下一财年"}营收</span><strong>{formatConsensusAmount(marketCapBridge.revenue, marketCapBridge.currency || "USD")}</strong></article>
                  <b>×</b>
                  <article><span>一致预期净利率</span><strong>{formatPlainPercent(marketCapBridge.net_margin_pct)}</strong></article>
                  <b>=</b>
                  <article><span>一致预期净利润</span><strong>{formatConsensusAmount(marketCapBridge.net_income, marketCapBridge.currency || "USD")}</strong></article>
                  <b>×</b>
                  <article><span>市场当前远期PE</span><strong>{formatMultiple(marketCapBridge.forward_pe)}</strong></article>
                  <b>=</b>
                  <article className="is-result"><span>当前可支撑市值</span><strong>{formatConsensusAmount(marketCapBridge.market_cap_from_profit, marketCapBridge.currency || "USD")}</strong></article>
                </div>
                <div className="industry-company-market-cap-note"><span>{marketCapBridge.formula}</span><b>PS交叉校验 {formatMultiple(marketCapBridge.forward_ps)}</b><small>{marketCapBridge.note}</small></div>
              </> : null}
              {supportDrivers.length ? <div className="industry-company-driver-grid">
                {supportDrivers.map((driver) => <article key={driver.key} className={`driver-${driver.status}`}>
                  <header><Tag>{driver.group}</Tag><Tag color={supportDriverColor(driver.status)}>{driver.status}</Tag></header>
                  <span>{driver.name}</span>
                  <strong>{formatSupportDriverValue(driver)}</strong>
                  <small>{supportDriverSecondary(driver)}</small>
                  <p>{driver.formula_role}</p>
                  <footer><b>继续支撑：</b>{driver.support_condition}<br /><em>转弱条件：</em>{driver.invalidation}<i>{driver.source}</i></footer>
                </article>)}
              </div> : null}
              {companyValuationFactors.length ? <details className="industry-company-business-drivers" open>
                <summary>业务因子监控 <span>{companyValuationFactors.length}项 · 用公司财报、订单、价格、供给和客户验证一致预期</span></summary>
                <div>
                  {companyValuationFactors.map((factor) => {
                    const [group, name] = factor.name.includes("｜") ? factor.name.split("｜", 2) : ["总模型", factor.name];
                    return <article key={factor.id}>
                      <header><Tag>{group}</Tag><strong>{name}</strong><Tag color={factor.status === "已确认" ? "green" : factor.status === "失败" ? "red" : "gold"}>{factor.status}</Tag></header>
                      <p>{factor.current_result || "当前结果待补充"}</p>
                      <footer><span><b>支撑条件：</b>{factor.criteria || "待设置"}</span>{factor.source_url ? <a href={factor.source_url} target="_blank" rel="noreferrer">{factor.source_name || "查看来源"}</a> : <i>{factor.source_name || "来源待补"}</i>}</footer>
                    </article>;
                  })}
                </div>
              </details> : null}
              <div className="industry-company-driver-rule"><strong>更新方式</strong><span>营收、EPS、预期修订和估值随“更新预期”刷新；业务因子进入产业更新中心复核，新增材料只更新因子状态，不自动覆盖正式预期差结论。</span></div>
            </div>
          ) : null}
          <div className="industry-company-expectation-flow">
            <article><b>1</b><span>市场当前隐含什么</span><p>{company.market_implied_expectation || automaticMarketExpectation || "待反推：当前市值对应多少收入、利润、份额和兑现年份。"}</p></article>
            <ArrowRightOutlined />
            <article className="is-evidence"><b>2</b><span>硬证据支持什么</span><p>{company.evidence_based_expectation || directEvidenceSummary || "待填写：只采用公告、财报、订单、价格、客户认证和利润兑现证据。"}</p></article>
            <ArrowRightOutlined />
            <article className={`is-result status-${gapStatus}`}><b>3</b><span>差额结论</span><strong>{gapLabel}</strong><p>{company.expectation_gap_reason || (marketPricingReady ? "一致预期和市值已经就绪；还需把硬证据量化到同一财年，才能判断正向或负向预期差。" : "两边口径未补齐，不能仅凭股价涨跌判断预期差。")}</p></article>
          </div>
          {calibrationGate ? <div className="industry-company-calibration-gate">
            <header><div><strong>五年回测校准</strong><span>{calibrationGate.signal_date ? `首次判断冻结于 ${calibrationGate.signal_date}` : "需要先冻结首次判断日"}</span></div><Tag color={calibrationGate.decision_code === "A" ? "green" : calibrationGate.decision_code === "B" ? "gold" : calibrationGate.decision_code === "C" ? "blue" : calibrationGate.decision_code === "D" ? "red" : "default"}>{calibrationGate.label}</Tag></header>
            <div className="industry-company-calibration-stages">
              <article><span>硬证据门</span><strong>{calibrationGate.evidence.state}</strong><small>增速 {formatPercent(calibrationGate.evidence.growth_pct)} / 门槛 {formatPlainPercent(calibrationGate.evidence.threshold_growth_pct)}</small><small>加速度 {formatPercent(calibrationGate.evidence.acceleration_pct)} / 门槛 {formatPlainPercent(calibrationGate.evidence.threshold_acceleration_pct)}</small><em>{calibrationGate.evidence.hard_source_count}条公司级硬来源</em></article>
              <article><span>披露前定价</span><strong>{calibrationGate.pricing.state}</strong><small>前20日超额 {formatPercent(calibrationGate.pricing.pre_20d_excess_pct)}</small><small>未定价门槛 ≤ {formatPlainPercent(calibrationGate.pricing.unpriced_threshold_pct)}</small></article>
              <article><span>首日确认</span><strong>{calibrationGate.confirmation.state}</strong><small>{calibrationGate.confirmation.observation_date || "交易日待到达"}</small><small>超额 {formatPercent(calibrationGate.confirmation.excess_pct)} / 门槛 {formatPlainPercent(calibrationGate.confirmation.threshold_pct)}</small></article>
              <article><span>执行状态</span><strong>{calibrationGate.execution.state === "next_session_available" ? "下一交易日可观察执行" : "尚未进入执行"}</strong><small>{calibrationGate.execution.entry_date || "没有生成交易动作"}</small><small>正式预期差：{calibrationGate.formal_gap_status}</small></article>
            </div>
            <p>{calibrationGate.reason}</p>
            <footer>{company.market === "美股" ? "美股方法未通过留出样本，只观察，不套用A股阈值。" : methodCalibration?.markets?.["A股"]?.usage || "A股校准结论待加载。"}</footer>
          </div> : null}
          <div className="industry-company-expectation-action"><Tag color={gapStatus === "负向预期差" ? "red" : needsReview ? "orange" : gapStatus === "正向预期差" ? "green" : undefined}>当前动作</Tag><strong>{gapAction}</strong></div>
          <div className="industry-company-expectation-guards">
            <article><span>升级触发器</span><p>{company.expectation_trigger || "尚未设置。正向预期差没有时间和指标触发器时，只能留在观察。"}</p></article>
            <article className="is-risk"><span>证伪条件</span><p>{company.expectation_invalidation || company.main_risk || "尚未设置。订单、价格、收入、毛利或现金流不兑现时必须下调判断。"}</p></article>
          </div>
          <div className="industry-company-expectation-rule"><strong>更新规则</strong><span>硬证据上修而市值未动，预期差扩大；市值先涨而证据未变，预期差收窄；市值或判断时间变化明显时只提示复核，不自动改写正式结论。</span></div>
        </section>

        <section className="industry-company-section">
          <header><div><strong>在产业链哪里</strong><span>先看它靠哪个环节挣钱，不把概念关联当主营业务</span></div><Tag>{nodes.length} 个环节</Tag></header>
          <div className="industry-company-node-list">
            {nodes.length ? nodes.map((node) => <Tag key={node.id} color={node.investment_importance === "强" ? "green" : undefined}>{node.name}</Tag>) : <span>尚未关联产业节点</span>}
          </div>
          <div className="industry-company-explanation-grid">
            <article><span>产业位置</span><p>{company.position || "待补充"}</p></article>
            <article><span>利润怎样兑现</span><p>{company.profit_path || "尚未说明订单、收入和利润的传导路径。"}</p></article>
            <article className="is-risk"><span>主要风险与证伪</span><p>{company.main_risk || "尚未设置公司级证伪条件。"}</p></article>
            <article><span>为什么被列入</span><p>{company.primary_reason || company.company_standing || "尚未填写比较依据。"}</p></article>
          </div>
        </section>

        {company.market === "A股" ? (
          <section className="industry-company-section">
            <header><div><strong>盘面表达</strong><span>行情只能确认资金选择，不能替代产业证据</span></div>{market?.trade_date ? <Tag>{market.trade_date}</Tag> : null}</header>
            {market ? <div className="industry-company-market-grid">
              <div><span>现价</span><strong>{market.close.toFixed(2)}</strong><small className={(market.change_pct ?? 0) >= 0 ? "market-up" : "market-down"}>{formatPercent(market.change_pct)}</small></div>
              <div><span>5日</span><strong>{formatPercent(market.return_5d_pct)}</strong></div>
              <div><span>20日</span><strong>{formatPercent(market.return_20d_pct)}</strong></div>
              <div><span>成交额</span><strong>{formatAmount(market.amount)}</strong></div>
            </div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无本地行情，不据此判断定价程度" />}
          </section>
        ) : null}

        <div className="industry-company-proof-grid">
          <section className="industry-company-section">
            <header><div><strong>验证与催化</strong><span>后面出现什么，才能升级或证伪判断</span></div><Tag>{validations.length + catalysts.length} 条</Tag></header>
            <div className="industry-company-compact-list">
              {validations.slice(0, 3).map((item) => <article key={`validation-${item.id}`}><Tag color={item.status === "已确认" ? "green" : item.status === "失败" ? "red" : "gold"}>{item.status}</Tag><div><strong>{item.name}</strong><p>{item.current_result || item.criteria || "等待验证"}</p></div></article>)}
              {catalysts.slice(0, 2).map((item) => <article key={`catalyst-${item.id}`}><Tag color="blue">{item.status}</Tag><div><strong>{item.event_name}</strong><p>{item.expected_time || "时间待定"} · {item.impact || "影响待补充"}</p></div></article>)}
              {!validations.length && !catalysts.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未设置公司或关联环节验证" /> : null}
            </div>
          </section>
          <section className="industry-company-section">
            <header><div><strong>关联硬证据</strong><span>这里只展示公司所在环节的来源，不等于全部直接证明公司受益</span></div><Tag>{sources.length} 条</Tag></header>
            <div className="industry-company-source-list">
              {sources.slice(0, 5).map((source) => <article key={source.id}><div><Tag color={["官方硬证据", "公司披露", "行业标准"].includes(source.source_tier) ? "green" : "blue"}>{source.source_tier}</Tag><span>{source.evidence_date}</span></div>{source.source_url ? <a href={source.source_url} target="_blank" rel="noreferrer">{source.title}</a> : <strong>{source.title}</strong>}<small>{source.source_name || "来源待补充"} · {source.verification_status}</small></article>)}
              {!sources.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该公司关联环节尚未绑定来源" /> : null}
            </div>
          </section>
        </div>
      </div>
    </Modal>
  );
}

function NodeDetailView({
  industryName,
  node,
  companies,
  validations,
  sources,
  onViewCompany
}: {
  industryName: string;
  node: IndustryTrendNode;
  companies: IndustryTrendCompany[];
  validations: IndustryTrendValidation[];
  sources: IndustryTrendSource[];
  onViewCompany: (company: IndustryTrendCompany) => void;
}) {
  const typeMeta = nodeTypeMeta(industryName, node.node_type);
  const hardSources = sources.filter((source) => ["官方硬证据", "公司披露", "行业标准"].includes(source.source_tier));
  const confirmedValidations = validations.filter((item) => item.status === "已确认").length;
  const readable = (value: string | null, fallback: string) => value || fallback;
  const companyDisplayLimit = node.name === "可重复使用运载火箭" ? companies.length : 8;

  return (
    <div className="industry-node-detail-view">
      <section className="industry-node-detail-overview">
        <div>
          <span>节点定位</span>
          <strong>{typeMeta.question}</strong>
          <p>{typeMeta.description}</p>
        </div>
        <div className="industry-node-detail-stats">
          <div><strong>{companies.length}</strong><span>关联公司</span></div>
          <div><strong>{hardSources.length}</strong><span>硬依据</span></div>
          <div><strong>{confirmedValidations}/{validations.length}</strong><span>验证确认</span></div>
        </div>
      </section>

      <section className="industry-node-detail-section is-reading">
        <div className="industry-node-detail-heading">
          <div><span>一眼看懂</span><small>先理解商业逻辑，再判断是否值得跟踪</small></div>
        </div>
        <div className="industry-node-read-flow">
          <article>
            <b>1</b><span>这是什么</span>
            <p>{readable(node.plain_explanation, "尚未补充通俗解释")}</p>
          </article>
          <article className="is-value">
            <b>2</b><span>钱为什么流到这里</span>
            <p>{readable(node.value_flow, "尚未说明需求如何变成订单和利润")}</p>
          </article>
          <article className="is-check">
            <b>3</b><span>后面看什么确认</span>
            <p>{readable(node.watch_signal, "尚未设置订单、价格、认证或利润验证信号")}</p>
          </article>
        </div>
      </section>

      <section className="industry-node-detail-section">
        <div className="industry-node-detail-heading">
          <div><span>投资判断</span><small>空间决定上限，壁垒与竞争决定利润留在哪里</small></div>
          <Space wrap size={5}>
            <Tag color="gold">利润弹性 {node.profit_elasticity || "待判断"}</Tag>
            <Tag color="cyan">国产替代 {node.localization || "待判断"}</Tag>
            <Tag color="green">投资重要性 {node.investment_importance || "待判断"}</Tag>
          </Space>
        </div>
        <div className="industry-node-judgement-view">
          <article><span>应用场景与市场空间</span><p>{readable(node.market_space, "尚未补充")}</p></article>
          <article><span>技术与交付壁垒</span><p>{readable(node.tech_barrier, "尚未补充")}</p></article>
          <article className="is-wide"><span>竞争格局</span><p>{readable(node.competition, "尚未补充")}</p></article>
        </div>
      </section>

      <section className="industry-node-detail-section">
        <div className="industry-node-detail-heading">
          <div><span>关联公司</span><small>谁在这个环节兑现收入与利润</small></div>
          <Tag>{companies.length} 家</Tag>
        </div>
        {companies.length ? (
          <div className="industry-node-company-grid">
            {companies.slice(0, companyDisplayLimit).map((company) => (
              <button className="industry-node-company-card" type="button" key={company.id} onClick={() => onViewCompany(company)}>
                <div><strong>{company.name}</strong><Tag color={company.market === "A股" ? "red" : company.market === "美股" ? "blue" : "cyan"}>{company.market}</Tag></div>
                <p>{company.position || company.company_standing || "产业位置待补充"}</p>
                <small>{company.core_advantage || company.profit_path || "核心优势与利润路径待补充"}</small>
                <footer>{company.is_global_leader ? <Tag color="purple">全球绝对优势</Tag> : null}{company.is_domestic_alternative ? <Tag color="cyan">国内可替代</Tag> : null}<Tag color={company.tracking_status === "核心受益" ? "gold" : undefined}>{company.tracking_status}</Tag><Tag color={company.verification_status === "已确认" ? "green" : company.verification_status === "失败" ? "red" : "blue"}>{company.verification_status}</Tag><Tag color={expectationGapColor(company.expectation_gap_status)}>{company.expectation_gap_status || "无法判断"}</Tag>{company.is_primary ? <Tag color="orange">A股交易首选</Tag> : null}</footer>
                <span className="industry-node-company-open">查看公司详情 →</span>
              </button>
            ))}
            {companies.length > companyDisplayLimit ? <div className="industry-node-more-count">其余 {companies.length - companyDisplayLimit} 家请在下方全球公司池查看</div> : null}
          </div>
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该节点尚未关联公司" />}
      </section>

      <div className="industry-node-proof-grid">
        <section className="industry-node-detail-section">
          <div className="industry-node-detail-heading"><div><span>验证进度</span><small>判断逻辑有没有兑现</small></div></div>
          {validations.length ? <div className="industry-node-validation-list">{validations.slice(0, 6).map((item) => (
            <article key={item.id}>
              <Tag color={item.status === "已确认" ? "green" : item.status === "失败" ? "red" : item.status === "验证中" ? "blue" : undefined}>{item.status}</Tag>
              <div><strong>{item.name}</strong><p>{item.current_result || item.criteria || "等待补充验证结果"}</p></div>
            </article>
          ))}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未设置节点验证项" />}
        </section>

        <section className="industry-node-detail-section">
          <div className="industry-node-detail-heading"><div><span>依据来源</span><small>优先阅读硬事实与交叉验证</small></div><Tag color={hardSources.length >= 2 ? "green" : hardSources.length ? "gold" : undefined}>{hardSources.length >= 2 ? "已有硬证据" : hardSources.length ? "单一硬来源" : "待补硬依据"}</Tag></div>
          {sources.length ? <div className="industry-node-source-view">{sources.slice(0, 6).map((source) => (
            <article key={source.id}>
              <div><Tag color={source.source_tier === "市场数据" || source.source_tier === "雪球线索" ? "blue" : "green"}>{source.source_tier}</Tag><span>{source.evidence_date}</span></div>
              {source.source_url ? <a href={source.source_url} target="_blank" rel="noreferrer">{source.title}</a> : <strong>{source.title}</strong>}
              <small>{source.source_name || "来源待补充"} · {source.verification_status}</small>
            </article>
          ))}</div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该节点尚未绑定证据" />}
        </section>
      </div>
    </div>
  );
}

function PCBMapOverview({ detail, onView }: { detail: IndustryTrendDetail; onView: (item: IndustryTrendNode) => void }) {
  const nodeByName = new Map(detail.nodes.map((node) => [node.name, node]));
  const validationStatus = (items: IndustryTrendValidation[]): VerificationStatus => {
    if (items.some((item) => item.status === "失败")) return "失败";
    if (items.length && items.every((item) => item.status === "已确认")) return "已确认";
    if (items.some((item) => item.status === "验证中" || item.status === "已确认")) return "验证中";
    return "未验证";
  };
  const demandValidations = detail.validations.filter((item) => item.name.includes("需求"));
  const orderValidations = detail.validations.filter((item) => item.name.includes("规模量产") || item.name.includes("收入结构"));
  const profitValidations = detail.validations.filter((item) => !demandValidations.includes(item) && !orderValidations.includes(item));
  const proofSteps = [
    { name: "需求源头", source: "季度财报 / Capex指引 / AI服务器出货", note: "先证明云厂商和服务器厂真的增加采购，而不是只看PCB公司表态。", status: validationStatus(demandValidations) },
    { name: "订单传导", source: "季报、中报 / 订单 / 客户认证", note: "确认需求已经传到服务器板、交换机板及高端料号。", status: validationStatus(orderValidations) },
    { name: "利润兑现", source: "中报、年报 / ASP / 毛利 / 现金流 / 良率", note: "最后确认涨价和产品升级没有被原料、折旧与低良率吃掉。", status: validationStatus(profitValidations) }
  ];
  const branches = [
    {
      key: "server",
      title: "AI服务器 / GPU计算板",
      question: "单机PCB价值量怎样提升？",
      companyNames: ["胜宏科技", "金像电子", "欣兴电子", "揖斐电（Ibiden）"],
      nodeNames: ["AI服务器与GPU集群扩张", "AI服务器主板与加速卡PCB", "HDI与高密度互连板", "高速低损耗覆铜板（CCL）", "高端PCB产能/良率/海外交付"]
    },
    {
      key: "switch",
      title: "高速交换机 / 网络板",
      question: "端口升级怎样变成高多层价值？",
      companyNames: ["沪电股份", "深南电路", "TTM Technologies", "台光电子"],
      nodeNames: ["800G/1.6T高速交换网络", "高速交换机与路由器PCB", "高多层高速PCB", "低介电电子布与电子纱", "高端PCB产能/良率/海外交付"]
    }
  ];
  const sharedBottlenecks = ["高速低损耗覆铜板（CCL）", "低介电电子布与电子纱", "高速铜箔与树脂体系", "激光钻孔/成型/检测设备", "高端PCB产能/良率/海外交付"]
    .map((name) => nodeByName.get(name))
    .filter((item): item is IndustryTrendNode => Boolean(item));
  const statusColor = (status: VerificationStatus) => status === "已确认" ? "green" : status === "失败" ? "red" : status === "验证中" ? "blue" : undefined;

  return (
    <div className="pcb-map-overview">
      <section className="pcb-proof-ladder">
        <div className="pcb-map-overview-heading"><div><strong>先确认需求，不只看年报</strong><span>需求、订单、利润必须逐层验证；前一层成立不代表后一层一定兑现</span></div></div>
        <div className="pcb-proof-steps">
          {proofSteps.map((step, index) => <article key={step.name}>
            <div><b>{index + 1}</b><strong>{step.name}</strong><Tag color={statusColor(step.status)}>{step.status}</Tag></div>
            <span>{step.source}</span>
            <p>{step.note}</p>
          </article>)}
        </div>
      </section>

      <section className="pcb-branch-map">
        <div className="pcb-map-overview-heading"><div><strong>再分两条赚钱路径</strong><span>服务器板重高阶HDI与大尺寸良率；交换机板重高多层、低损耗与背板经验</span></div><Tag color="orange">不可混为一种PCB</Tag></div>
        <div className="pcb-branch-list">
          {branches.map((branch) => {
            const nodes = branch.nodeNames.map((name) => nodeByName.get(name)).filter((item): item is IndustryTrendNode => Boolean(item));
            const companies = branch.companyNames.map((name) => detail.companies.find((company) => company.name === name)).filter((item): item is IndustryTrendCompany => Boolean(item));
            return <article className={`pcb-branch-row is-${branch.key}`} key={branch.key}>
              <div className="pcb-branch-label"><strong>{branch.title}</strong><span>{branch.question}</span><div>{companies.map((company) => <Tag key={company.id} color={company.market === "A股" ? "red" : undefined}>{company.name}</Tag>)}</div></div>
              <div className="pcb-branch-path">{nodes.map((node, index) => <div key={node.id}>{index ? <ArrowRightOutlined /> : null}<button type="button" onClick={() => onView(node)}><span>{node.name}</span><small>{node.value_flow || node.plain_explanation}</small></button></div>)}</div>
            </article>;
          })}
        </div>
        <div className="pcb-shared-bottlenecks"><div><strong>共同材料与量产瓶颈</strong><span>决定性能、涨价和最终毛利</span></div><div>{sharedBottlenecks.map((node) => <Button key={node.id} size="small" onClick={() => onView(node)}>{node.name}</Button>)}</div></div>
      </section>
    </div>
  );
}

type ProductionItem = { label: string; node: string; note: string };
type ProductionGroup = { title: string; question: string; items: ProductionItem[] };
type ProductionStep = { title: string; note: string; nodes: string[] };

function ProductionBlueprint({ detail, onView }: { detail: IndustryTrendDetail; onView: (item: IndustryTrendNode) => void }) {
  const isPCB = isPCBIndustry(detail.name);
  const isOptical = isOpticalIndustry(detail.name);
  if (!isPCB && !isOptical) return null;

  const nodeByName = new Map(detail.nodes.map((node) => [node.name, node]));
  const pcbGroups: ProductionGroup[] = [
    {
      title: "板材骨架",
      question: "板子由什么叠起来？",
      items: [
        { label: "覆铜板 CCL", node: "高速低损耗覆铜板（CCL）", note: "铜箔与绝缘材料压合成的基础板材" },
        { label: "半固化片与芯板", node: "半固化片（Prepreg）与芯板", note: "把多层线路粘合、绝缘并支撑起来" },
        { label: "电子布与电子纱", node: "低介电电子布与电子纱", note: "提供强度，并影响介电与传输损耗" },
        { label: "铜箔与树脂", node: "高速铜箔与树脂体系", note: "决定导电、粗糙度、耐热和损耗" }
      ]
    },
    {
      title: "成像与表面",
      question: "线路怎样被做出来？",
      items: [
        { label: "干膜 / 光刻胶 / 阻焊", node: "干膜/光刻胶与阻焊油墨", note: "负责线路成像与成品保护" },
        { label: "电镀 / 蚀刻 / 表面处理", node: "电镀铜/蚀刻与表面处理化学品", note: "形成铜线路、孔铜和焊接表面" }
      ]
    },
    {
      title: "加工耗材",
      question: "哪些东西持续消耗？",
      items: [
        { label: "钻针 / 铣刀 / 盖垫板", node: "钻针/铣刀/盖垫板等耗材", note: "钻孔、外形成型和保护板面所需耗材" }
      ]
    },
    {
      title: "生产设备",
      question: "靠什么机器量产？",
      items: [
        { label: "压合 / LDI / 电镀产线", node: "压合/LDI曝光/电镀产线", note: "完成叠层、线路曝光和孔铜沉积" },
        { label: "激光钻孔 / 成型", node: "激光钻孔/成型/检测设备", note: "加工微孔、外形并进行自动检测" },
        { label: "AOI / 电测 / 可靠性", node: "AOI/电测与可靠性终检", note: "在出货前排除短路、开路与可靠性风险" }
      ]
    }
  ];
  const pcbSteps: ProductionStep[] = [
    { title: "开料与内层成像", note: "在芯板上形成内层线路", nodes: ["干膜/光刻胶与阻焊油墨", "高速低损耗覆铜板（CCL）"] },
    { title: "多层压合", note: "用半固化片叠成多层板", nodes: ["半固化片（Prepreg）与芯板", "压合/LDI曝光/电镀产线"] },
    { title: "机械钻孔 / 激光微孔", note: "让不同线路层可以导通", nodes: ["激光钻孔/成型/检测设备", "钻针/铣刀/盖垫板等耗材"] },
    { title: "沉铜与电镀", note: "在孔壁形成可靠导电铜层", nodes: ["电镀铜/蚀刻与表面处理化学品", "压合/LDI曝光/电镀产线"] },
    { title: "外层成像与蚀刻", note: "留下需要的外层铜线路", nodes: ["干膜/光刻胶与阻焊油墨", "电镀铜/蚀刻与表面处理化学品"] },
    { title: "阻焊与表面处理", note: "保护线路并形成焊接表面", nodes: ["干膜/光刻胶与阻焊油墨", "电镀铜/蚀刻与表面处理化学品"] },
    { title: "成型、AOI与电测", note: "检查线路、外形和电气性能", nodes: ["AOI/电测与可靠性终检", "激光钻孔/成型/检测设备"] },
    { title: "认证与良率爬坡", note: "通过客户验证后才进入利润兑现", nodes: ["高端PCB产能/良率/海外交付"] }
  ];
  const opticalGroups: ProductionGroup[] = [
    {
      title: "电芯片",
      question: "电信号怎样被处理？",
      items: [
        { label: "DSP / CDR", node: "光DSP/CDR", note: "完成高速信号恢复、补偿与编解码" },
        { label: "TIA / Driver", node: "TIA/Driver模拟芯片", note: "驱动激光器并放大探测器的微弱电流" },
        { label: "MCU / EEPROM / 电源", node: "MCU/EEPROM/电源管理芯片", note: "负责控制、身份信息、监控与供电" }
      ]
    },
    {
      title: "发光与收光",
      question: "光从哪里来、怎样被收到？",
      items: [
        { label: "EML / CW 激光器", node: "InP EML/CW激光器", note: "产生并调制高速光信号" },
        { label: "硅光 PIC / 调制器", node: "硅光PIC与调制器", note: "把光路集成到芯片上" },
        { label: "PD / APD 探测器", node: "高速光探测器PD/APD", note: "把接收到的光转换为电信号" }
      ]
    },
    {
      title: "无源光学",
      question: "光怎样合分、聚焦与连接？",
      items: [
        { label: "AWG / PLC / WDM", node: "AWG/PLC/WDM无源器件", note: "把不同波长的光合并或拆开" },
        { label: "透镜 / 隔离器 / 滤波片", node: "透镜/隔离器/滤波片等微光学件", note: "聚焦、保护并筛选光路" },
        { label: "光纤阵列 / MPO", node: "光纤阵列/MPO连接器", note: "把模块稳定连接到外部光纤" }
      ]
    },
    {
      title: "载板、结构与散热",
      question: "器件怎样装进一个模块？",
      items: [
        { label: "PCB / FPC / 高速电连接", node: "光模块PCB/FPC与高速电连接", note: "承载芯片并连接主机侧高速信号" },
        { label: "OSFP / QSFP 壳体与连接器", node: "OSFP/QSFP壳体/散热器/连接器", note: "提供机械接口、屏蔽和散热通道" },
        { label: "TEC 温控与热界面", node: "TEC温控与热界面材料", note: "稳定激光器温度并把热量导出" }
      ]
    }
  ];
  const opticalSteps: ProductionStep[] = [
    { title: "SMT贴片与芯片键合", note: "把电芯片和光芯片装到载板", nodes: ["SMT贴片与芯片键合", "光模块PCB/FPC与高速电连接"] },
    { title: "主动 / 被动光学耦合", note: "把激光、透镜与光纤精确对准", nodes: ["精密耦合与光学封装", "透镜/隔离器/滤波片等微光学件"] },
    { title: "光纤管理与壳体封装", note: "固定光路、连接器并完成散热结构", nodes: ["光纤阵列/MPO连接器", "OSFP/QSFP壳体/散热器/连接器"] },
    { title: "固件与CMIS校准", note: "烧录、参数校准并与交换机通信", nodes: ["模块固件/CMIS校准与功能测试", "MCU/EEPROM/电源管理芯片"] },
    { title: "高速误码与温度测试", note: "验证速率、功耗和全温性能", nodes: ["高速测试与老化筛选", "模块固件/CMIS校准与功能测试"] },
    { title: "老化筛选与可靠性交付", note: "剔除早期失效，确认批量一致性", nodes: ["高速测试与老化筛选"] }
  ];
  const groups = isPCB ? pcbGroups : opticalGroups;
  const steps = isPCB ? pcbSteps : opticalSteps;
  const nodeCount = new Set(groups.flatMap((group) => group.items.map((item) => item.node)).filter((name) => nodeByName.has(name))).size;

  return (
    <section className={`production-blueprint ${isPCB ? "is-pcb" : "is-optical"}`}>
      <div className="production-blueprint-heading">
        <div><strong>{isPCB ? "PCB怎么生产" : "一个光模块需要什么"}</strong><span>{isPCB ? "从板材、化学品和耗材，一直看到成品检测" : "先看模块BOM，再看器件怎样被组装、校准和测试"}</span></div>
        <Tag color="blue">{nodeCount} 个生产节点</Tag>
      </div>
      <div className="production-bom-grid">
        {groups.map((group) => <article key={group.title}>
          <header><strong>{group.title}</strong><span>{group.question}</span></header>
          <div>{group.items.map((item) => {
            const node = nodeByName.get(item.node);
            return <button key={item.node} type="button" disabled={!node} onClick={() => node && onView(node)}><strong>{item.label}</strong><span>{item.note}</span><small>{node ? "查看公司、依据与验证 →" : "节点待补充"}</small></button>;
          })}</div>
        </article>)}
      </div>
      <div className="production-process">
        <div className="production-process-title"><strong>制造流程</strong><span>{isPCB ? "一块高速多层PCB从材料到出货" : "器件进入工厂后怎样变成可交付模块"}</span></div>
        <div className="production-process-steps">{steps.map((step, index) => {
          const linkedNodes = step.nodes.map((name) => nodeByName.get(name)).filter((node): node is IndustryTrendNode => Boolean(node));
          return <div key={step.title} className="production-process-step">
            <button type="button" disabled={!linkedNodes.length} onClick={() => linkedNodes[0] && onView(linkedNodes[0])}><b>{index + 1}</b><strong>{step.title}</strong><span>{step.note}</span></button>
            {index < steps.length - 1 ? <ArrowRightOutlined /> : null}
          </div>;
        })}</div>
      </div>
      <small className="production-blueprint-footnote">点任一材料或工序，可继续查看关联公司、硬证据、当前验证状态和后续跟踪信号。</small>
    </section>
  );
}

function NodeMap({
  detail,
  onView,
  onViewCompany,
  onEdit,
  onDelete,
  onAdd,
  onEdges,
  onReorder
}: {
  detail: IndustryTrendDetail;
  onView: (item: IndustryTrendNode) => void;
  onViewCompany: (company: IndustryTrendCompany) => void;
  onEdit: (item: IndustryTrendNode) => void;
  onDelete: (item: IndustryTrendNode) => void;
  onAdd: (type?: NodeType) => void;
  onEdges: () => void;
  onReorder: (from: IndustryTrendNode, to: IndustryTrendNode) => void;
}) {
  const dragItem = useRef<IndustryTrendNode | null>(null);
  const [viewMode, setViewMode] = useState<"main" | "all">("main");
  const [expandedNodeIds, setExpandedNodeIds] = useState<Set<number>>(() => new Set());
  const isPCB = isPCBIndustry(detail.name);
  const isOptical = isOpticalIndustry(detail.name);
  const isCloud = isCloudIndustry(detail.name);
  const isDomesticCompute = isDomesticComputeIndustry(detail.name);
  const visibleNodes = viewMode === "main"
    ? detail.nodes.filter((node) => isPCB ? node.investment_importance !== "弱" : node.investment_importance === "强")
    : detail.nodes;
  const maturityColor = (status: NodeMaturityStatus | null) => {
    if (status === "成熟应用" || status === "放量中") return "green";
    if (status === "验证中" || status === "小批量") return "gold";
    return "default";
  };
  return (
    <section className="trend-terminal-section trend-map-section">
      <div className="trend-section-title">
        <div><Typography.Title level={4}>投资型产业链地图</Typography.Title><Typography.Text>沿着“谁花钱 → 买什么 → 哪些产品收钱 → 谁掌握瓶颈 → 谁能交付”阅读</Typography.Text></div>
        <Space wrap><Segmented size="small" value={viewMode} onChange={(value) => setViewMode(value as "main" | "all")} options={[{ label: "主线图", value: "main" }, { label: `完整细分 ${detail.nodes.length}`, value: "all" }]} /><Button size="small" icon={<LinkOutlined />} onClick={onEdges}>管理连线</Button><Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => onAdd()}>新增节点</Button></Space>
      </div>
      <div className="trend-map-beginner-guide">
        <strong>{isPCB ? "PCB先这样看：" : isCloud ? "云计算与算力租赁先这样看：" : isDomesticCompute ? "国产算力先这样看：" : "第一次看这个行业："}</strong>
        <span>{isPCB ? "先分服务器板与交换机板" : isCloud ? "谁愿意付费" : isDomesticCompute ? "先确认真实采购与扩容" : "先看前两步确认需求"}</span><ArrowRightOutlined />
        <span>{isPCB ? "再看HDI、高多层和背板" : isCloud ? "谁买卡建资源池、怎样出租" : isDomesticCompute ? "再看芯片怎样形成可用集群" : "再看第三步找收入"}</span><ArrowRightOutlined />
        <span>{isPCB ? "最后查材料、良率与交付" : isCloud ? "最后看出租率、租价、毛利和现金流" : isDomesticCompute ? "最后看订单、利用率与现金流" : "最后两步找利润弹性和供给瓶颈"}</span>
        <small>{isPCB ? "需求确认不等于利润兑现，必须继续看订单、ASP、毛利和良率。" : isCloud ? "云收入增长不等于算力租赁赚钱；设备折旧、利用率和回款必须单独验证。" : isDomesticCompute ? "芯片性能不等于有效算力；订单、集群利用率、毛利与现金流必须共同验证。" : "主线图只保留最重要节点；完整细分图用于深入研究。"}</small>
      </div>
      {isPCB || isOptical ? <ProductionBlueprint detail={detail} onView={onView} /> : null}
      {isPCB ? <PCBMapOverview detail={detail} onView={onView} /> : null}
      <div className="trend-node-map">
        {NODE_TYPES.map((type, typeIndex) => {
          const nodes = visibleNodes.filter((node) => node.node_type === type);
          const meta = nodeTypeMeta(detail.name, type);
          return (
            <div className="trend-node-lane" key={type}>
              <div className="trend-node-lane-heading">
                <div className="trend-node-lane-title"><span><b>{typeIndex + 1}</b><strong>{meta.title || type}</strong><small>{meta.question}</small></span><Button type="text" size="small" icon={<PlusOutlined />} onClick={() => onAdd(type)} /></div>
                <p className="trend-node-lane-help">{meta.description}</p>
              </div>
              <div className="trend-node-lane-body">
                {nodes.length ? nodes.map((node) => {
                  const companies = detail.companies.filter((company) => company.node_ids.includes(node.id));
                  const nodeCompanyDisplayLimit = node.name === "可重复使用运载火箭" ? companies.length : 3;
                  const nodeSources = detail.sources.filter((source) => source.node_ids.includes(node.id));
                  const hardSources = nodeSources.filter((source) => ["官方硬证据", "公司披露", "行业标准"].includes(source.source_tier));
                  const hardSourceCount = hardSources.length;
                  const independentHardSourceCount = new Set(hardSources.map((source) => source.source_name || source.source_url || source.title)).size;
                  const crossVerified = independentHardSourceCount >= 2;
                  const evidenceLabel = crossVerified ? "已互证" : hardSourceCount ? "单一来源" : nodeSources.length ? "市场线索" : "待补依据";
                  const expanded = expandedNodeIds.has(node.id);
                  return (
                  <div
                    key={node.id}
                    className={`trend-node-card${expanded ? " is-expanded" : ""}`}
                    draggable
                    role="button"
                    tabIndex={0}
                    onClick={() => onView(node)}
                    onKeyDown={(event) => { if (event.key === "Enter") onView(node); }}
                    onDragStart={() => { dragItem.current = node; }}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={() => { if (dragItem.current && dragItem.current.id !== node.id) onReorder(dragItem.current, node); dragItem.current = null; }}
                  >
                    <div className="trend-node-card-header">
                      <div className="trend-node-card-title"><strong>{node.name}</strong>{node.maturity_status ? <Tag color={maturityColor(node.maturity_status)}>{node.maturity_status}</Tag> : null}</div>
                      <Space className="trend-node-card-actions" size={0}><Button type="text" size="small" aria-label={`编辑${node.name}`} icon={<EditOutlined />} onClick={(event) => { event.stopPropagation(); onEdit(node); }} /><Button type="text" danger size="small" aria-label={`删除${node.name}`} icon={<DeleteOutlined />} onClick={(event) => { event.stopPropagation(); onDelete(node); }} /></Space>
                    </div>
                    <p className="trend-node-plain"><span className="trend-node-label">是什么</span><span className="trend-node-copy">{node.plain_explanation || node.market_space || "等待补充通俗解释"}</span></p>
                    {node.value_flow ? <p className="trend-node-value"><span className="trend-node-label">{isCloud ? "钱怎么流" : "赚什么"}</span><span className="trend-node-copy">{node.value_flow}</span></p> : null}
                    {companies.length ? <div className="trend-node-companies"><span>代表公司</span>{companies.slice(0, nodeCompanyDisplayLimit).map((company) => <button key={company.id} type="button" className={`trend-node-company-chip market-${company.market === "A股" ? "a-share" : "overseas"}`} aria-label={`查看${company.name}公司详情`} onClick={(event) => { event.stopPropagation(); onViewCompany(company); }}>{company.name}</button>)}{companies.length > nodeCompanyDisplayLimit ? <small>+{companies.length - nodeCompanyDisplayLimit}</small> : null}</div> : null}
                    <div className="trend-node-evidence-summary">
                      <Tag color={crossVerified ? "green" : hardSourceCount ? "gold" : nodeSources.length ? "blue" : undefined}>{evidenceLabel}</Tag>
                      <span>{hardSourceCount ? `${hardSourceCount}条硬依据` : nodeSources.length ? `${nodeSources.length}条线索` : "尚未绑定来源"}</span>
                    </div>
                    {(node.watch_signal || node.tech_barrier || node.profit_elasticity || node.localization || nodeSources.length) ? (
                      <div className="trend-node-more" onClick={(event) => event.stopPropagation()}>
                        <Button
                          type="text"
                          size="small"
                          onClick={() => setExpandedNodeIds((current) => {
                            const next = new Set(current);
                            if (next.has(node.id)) next.delete(node.id); else next.add(node.id);
                            return next;
                          })}
                        >{expanded ? "收起价值与验证" : "展开价值与验证"}</Button>
                        {expanded ? <div className="trend-node-more-content">
                          {node.watch_signal ? <p className="trend-node-watch"><span className="trend-node-label">看什么</span><span className="trend-node-copy">{node.watch_signal}</span></p> : null}
                          <Space wrap size={4}>
                            {node.tech_barrier ? <Tag>壁垒 {node.tech_barrier}</Tag> : null}
                            {node.profit_elasticity ? <Tag color="gold">弹性 {node.profit_elasticity}</Tag> : null}
                            {node.localization ? <Tag color="cyan">国产化 {node.localization}</Tag> : null}
                          </Space>
                          <div className="trend-node-source-list">
                            <strong>依据来源</strong>
                            {nodeSources.length ? nodeSources.map((source) => <div key={source.id}>
                              <Tag color={source.source_tier === "雪球线索" || source.source_tier === "市场数据" ? "blue" : "green"}>{source.source_tier}</Tag>
                              {source.source_url ? <a href={source.source_url} target="_blank" rel="noreferrer">{source.title}</a> : <span>{source.title}</span>}
                              <small>{source.evidence_date}</small>
                            </div>) : <span>该节点还没有绑定来源，暂按待验证处理。</span>}
                          </div>
                        </div> : null}
                      </div>
                    ) : null}
                  </div>
                  );
                }) : <div className="trend-node-empty">主线图暂无重点节点</div>}
              </div>
              {typeIndex < NODE_TYPES.length - 1 ? <ArrowRightOutlined className="trend-lane-arrow" /> : null}
            </div>
          );
        })}
      </div>
      {detail.edges.length ? (
        <div className="trend-edge-summary">
          <details><summary>{detail.edges.length} 条产业传导关系（展开查看）</summary><div>{detail.edges.map((edge) => {
            const from = detail.nodes.find((node) => node.id === edge.from_node_id)?.name;
            const to = detail.nodes.find((node) => node.id === edge.to_node_id)?.name;
            return <Tag key={edge.id}>{from || "?"} → {to || "?"}</Tag>;
          })}</div></details>
        </div>
      ) : null}
    </section>
  );
}

function causalStatusColor(value?: CausalSignalStatus | null) {
  if (value === "已确认") return "green";
  if (value === "减弱") return "orange";
  if (value === "失效") return "red";
  return "blue";
}

type DynamicsDriverKey = "change" | "attention" | "capital" | "expression" | "selling";

const DYNAMICS_DETAIL_TITLES: Record<DynamicsDriverKey, { title: string; subtitle: string }> = {
  change: { title: "真实变化明细", subtitle: "逐条查看硬事实、信息互证和仍待确认的支持证据" },
  attention: { title: "信息扩散明细", subtitle: "合并多类信息来源；雪球只作线索，不冒充全市场关注度" },
  capital: { title: "盘面资金明细", subtitle: "查看A股产业样本的涨跌、成交额变化和首选公司表达" },
  expression: { title: "首选公司表达", subtitle: "判断公司受益逻辑与盘面是否同时成立" },
  selling: { title: "潜在卖压明细", subtitle: "查看拥挤、兑现、反向证据及其发生时间" },
};

function dynamicsDetailTagColor(value: string) {
  if (["硬事实", "信息互证", "已确认", "verified", "cross_verified"].includes(value)) return "green";
  if (["部分验证", "旧硬事实", "市场线索", "partial", "线索"].includes(value)) return "gold";
  if (["减弱", "失效", "高"].includes(value)) return "red";
  return "blue";
}

function DynamicsDetailModal({ kind, detail, onClose }: {
  kind: DynamicsDriverKey | null;
  detail: IndustryTrendDetail;
  onClose: () => void;
}) {
  if (!kind) return null;
  const dynamics = detail.dynamics;
  const meta = DYNAMICS_DETAIL_TITLES[kind];
  const marketCompanies = detail.companies
    .filter((item) => item.market === "A股" && item.market_snapshot)
    .sort((left, right) => (right.market_snapshot?.return_5d_pct ?? -999) - (left.market_snapshot?.return_5d_pct ?? -999));
  const primaryCompany = detail.companies.find((item) => item.name === dynamics.expression.company);
  const sellSignals = detail.updates
    .filter((item) => item.sell_pressure && item.signal_status !== "失效")
    .sort((left, right) => right.update_date.localeCompare(left.update_date) || right.id - left.id);

  return <Modal
    centered
    width={900}
    open
    className="trend-dynamics-detail-modal"
    title={<div className="trend-dynamics-detail-title"><strong>{meta.title}</strong><span>{meta.subtitle}</span></div>}
    onCancel={onClose}
    footer={<Button onClick={onClose}>关闭</Button>}
  >
    <div className="trend-dynamics-detail-body">
      {kind === "change" ? <>
        <div className="trend-dynamics-detail-summary">
          <div><span>硬确认</span><strong>{dynamics.evidence.confirmed}</strong></div>
          <div><span>手工硬事实</span><strong>{dynamics.evidence.hard_facts}</strong></div>
          <div><span>信息互证</span><strong>{dynamics.evidence.verified_information}</strong></div>
          <div><span>支持证据</span><strong>{dynamics.evidence.supporting}</strong></div>
        </div>
        <div className="trend-dynamics-detail-list">
          {dynamics.evidence.items.length ? dynamics.evidence.items.map((item) => <article key={item.id}>
            <header><Tag color={dynamicsDetailTagColor(item.category)}>{item.category}</Tag><time>{item.published_at ? formatTime(item.published_at) : "时间待补"}</time></header>
            {item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.title}</a> : <strong>{item.title}</strong>}
            {item.summary ? <p>{item.summary}</p> : null}
            <small>{item.source_name} · {item.status}</small>
          </article>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可展开证据" />}
        </div>
      </> : null}

      {kind === "attention" ? <>
        <Alert
          showIcon
          type={dynamics.attention.coverage === "可用" ? "info" : "warning"}
          message={dynamics.attention.baseline_complete ? "多源信息扩散口径" : "当前只能看信息活跃度，不能判断升温或降温"}
          description={`近7日合并去重后共 ${dynamics.attention.sample_7d} 条、${dynamics.attention.source_count_7d} 个来源、${dynamics.attention.channel_count_7d} 类渠道。雪球只占 ${dynamics.attention.xq_7d} 条、${dynamics.attention.xq_author_count_7d} 位作者，仅作为市场线索。${dynamics.attention.baseline_complete ? "前后窗口口径可比。" : "信息筛选稳定基线尚不足14天，系统不会据此输出扩散或降温结论。"}`}
        />
        <div className="trend-dynamics-detail-summary">
          <div><span>近7日多源信息</span><strong>{dynamics.attention.sample_7d}</strong></div>
          <div><span>前7日同口径</span><strong>{dynamics.attention.sample_previous_7d}</strong></div>
          <div><span>独立来源</span><strong>{dynamics.attention.source_count_7d}</strong></div>
          <div><span>渠道覆盖</span><strong>{dynamics.attention.channel_count_7d} 类</strong></div>
        </div>
        <div className="trend-dynamics-detail-list">
          {dynamics.attention.items.length ? dynamics.attention.items.map((item) => <article key={`${item.category}-${item.id}`}>
            <header><Tag color={dynamicsDetailTagColor(item.category)}>{item.category}</Tag><time>{item.published_at ? formatTime(item.published_at) : "时间待补"}</time></header>
            {item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.title}</a> : <strong>{item.title}</strong>}
            <p>{item.summary}</p><small>{item.source_name} · {item.status}</small>
          </article>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配关注记录" />}
        </div>
      </> : null}

      {kind === "capital" ? <>
        <div className="trend-dynamics-detail-summary">
          <div><span>行情样本</span><strong>{dynamics.market.company_count}</strong></div>
          <div><span>5日上涨比例</span><strong>{formatPercent(dynamics.market.breadth_5d_pct)}</strong></div>
          <div><span>放量比例</span><strong>{formatPercent(dynamics.market.amount_expansion_pct)}</strong></div>
          <div><span>当前状态</span><strong>{dynamics.market.status}</strong></div>
        </div>
        <div className="trend-dynamics-detail-market">
          <header><span>公司</span><span>1日</span><span>5日</span><span>20日</span><span>量比</span></header>
          {marketCompanies.map((company) => {
            const snapshot = company.market_snapshot!;
            return <div key={company.id}>
              <span><b>{company.name}</b><small>{company.is_primary ? "A股交易首选" : company.tracking_status}</small></span>
              <span>{formatPercent(snapshot.change_pct)}</span><span>{formatPercent(snapshot.return_5d_pct)}</span>
              <span>{formatPercent(snapshot.return_20d_pct)}</span><span>{snapshot.amount_ratio_5d ? `${snapshot.amount_ratio_5d.toFixed(2)}×` : "-"}</span>
            </div>;
          })}
        </div>
      </> : null}

      {kind === "expression" ? primaryCompany ? <div className="trend-dynamics-expression-detail">
        <header><div><Tag color="green">{primaryCompany.is_primary ? "A股交易首选" : primaryCompany.tracking_status}</Tag><strong>{primaryCompany.name}</strong><span>{primaryCompany.full_code}</span></div><Tag>{dynamics.expression.status}</Tag></header>
        <div className="trend-dynamics-detail-summary">
          <div><span>5日表现</span><strong>{formatPercent(primaryCompany.market_snapshot?.return_5d_pct)}</strong></div>
          <div><span>量比</span><strong>{primaryCompany.market_snapshot?.amount_ratio_5d ? `${primaryCompany.market_snapshot.amount_ratio_5d.toFixed(2)}×` : "-"}</strong></div>
          <div><span>验证状态</span><strong>{primaryCompany.verification_status}</strong></div>
          <div><span>市场定价</span><strong>{primaryCompany.pricing_status}</strong></div>
        </div>
        <article><span>产业位置</span><strong>{primaryCompany.position || "待补充"}</strong></article>
        <article><span>为什么是它</span><p>{primaryCompany.core_advantage || primaryCompany.primary_reason || "待补充首选依据"}</p></article>
        <article><span>利润怎样兑现</span><p>{primaryCompany.profit_path || "待补充利润兑现路径"}</p></article>
      </div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未设置首选公司" /> : null}

      {kind === "selling" ? <>
        <div className="trend-dynamics-detail-summary">
          <div><span>当前卖压</span><strong>{dynamics.selling.level}</strong></div>
          <div className="is-wide"><span>当前判断</span><strong>{dynamics.selling.reason}</strong></div>
        </div>
        <div className="trend-dynamics-detail-list">
          {sellSignals.length ? sellSignals.map((item) => <article key={item.id}>
            <header><Tag color={dynamicsDetailTagColor(item.sell_pressure || "")}>{item.sell_pressure}卖压</Tag><Tag color={causalStatusColor(item.signal_status)}>{item.signal_status || "线索"}</Tag><time>{item.update_date}</time></header>
            <strong>{item.content}</strong>
            {item.counter_evidence ? <p>反向证据：{item.counter_evidence}</p> : null}
            <small>{item.source_name || "产业跟踪记录"}</small>
          </article>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无明确卖压记录" />}
        </div>
      </> : null}
    </div>
  </Modal>;
}

function CausalReplay({ detail, onAdd, onEdit }: {
  detail: IndustryTrendDetail;
  onAdd: (stage?: CausalStage) => void;
  onEdit: (item: IndustryTrendUpdate) => void;
}) {
  const [driverDetail, setDriverDetail] = useState<DynamicsDriverKey | null>(null);
  const signals = detail.updates.filter((item) => item.causal_stage);
  const sortedSignals = [...signals].sort((left, right) => left.update_date.localeCompare(right.update_date) || left.id - right.id);
  const latestBuyer = [...signals].sort((left, right) => right.update_date.localeCompare(left.update_date) || right.id - left.id).find((item) => item.buyer_group)?.buyer_group;
  const dynamics = detail.dynamics;
  const drivers = [
    {
      key: "change",
      label: "真实变化",
      status: dynamics.evidence.status,
      metric: `${dynamics.evidence.confirmed} 条硬确认`,
      detail: `手工硬事实 ${dynamics.evidence.hard_facts} · 信息互证 ${dynamics.evidence.verified_information} · 待互证 ${dynamics.evidence.supporting}`,
      state: dynamics.evidence.confirmed > 0 ? "pass" : "watch"
    },
    {
      key: "attention",
      label: "认知扩散",
      status: dynamics.attention.status,
      metric: `近7日 ${dynamics.attention.sample_7d} 条 · ${dynamics.attention.source_count_7d} 来源`,
      detail: `覆盖 ${dynamics.attention.channel_count_7d} 类渠道；雪球 ${dynamics.attention.xq_7d} 条/${dynamics.attention.xq_author_count_7d} 位作者，仅作线索`,
      state: dynamics.attention.status === "信息扩散" ? "pass" : "watch"
    },
    {
      key: "capital",
      label: "资金确认",
      status: dynamics.market.status,
      metric: `5日上涨比例 ${formatPercent(dynamics.market.breadth_5d_pct)}`,
      detail: `核心公司加权 · ${dynamics.market.company_count} 家样本 · 放量 ${formatPercent(dynamics.market.amount_expansion_pct)}`,
      state: ["资金确认", "龙头先行"].includes(dynamics.market.status) ? "pass" : dynamics.market.status === "盘面转弱" ? "danger" : "watch"
    },
    {
      key: "expression",
      label: "股票表达",
      status: dynamics.expression.status,
      metric: dynamics.expression.company || "暂无首选",
      detail: `${dynamics.expression.structural_ready ? "产业受益合格" : "产业受益待验证"} · 5日 ${formatPercent(dynamics.expression.return_5d_pct)}${latestBuyer ? ` · 买方 ${latestBuyer}` : ""}`,
      state: dynamics.expression.market_confirmed && dynamics.expression.structural_ready ? "pass" : "watch"
    },
    {
      key: "selling",
      label: "潜在卖压",
      status: dynamics.selling.level,
      metric: dynamics.selling.level === "高" ? "先防守，不和兑现盘硬碰" : "尚未出现决定性卖压",
      detail: dynamics.selling.reason,
      state: dynamics.selling.level === "高" ? "danger" : dynamics.selling.level === "中" ? "watch" : "pass"
    }
  ];

  return (
    <section className="trend-dynamics-workbench">
      <div className="trend-section-title">
        <div>
          <Typography.Title level={4}>上涨动力监控</Typography.Title>
          <Typography.Text>判断顺序固定：先确认真实变化，再看资金与首选公司，最后用卖压和关键证伪过滤。</Typography.Text>
        </div>
        <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => onAdd()}>记录新变化</Button>
      </div>

      <div className="trend-dynamics-rule">
        <strong>判定路径</strong>
        <span>硬变化</span><i>→</i><span>盘面资金</span><i>→</i><span>首选公司表达</span><i>→</i><span>卖压 / 关键证伪</span>
        <small>认知扩散采用多源去重样本；雪球只作线索，基线不足时不判断升温或降温。</small>
      </div>

      <div className={`trend-dynamics-action tone-${dynamics.action_tone}`}>
        <div className="trend-dynamics-action-verdict"><span>当前动作</span><strong>{dynamics.action}</strong><small>截至 {dynamics.as_of}</small></div>
        <div className="trend-dynamics-action-cards">
          <article className="trend-dynamics-action-reason"><span>判断依据</span><strong>{dynamics.action_reason}</strong><p>通过 {dynamics.decision_trace.passed.length} 项 · 等待 {dynamics.decision_trace.waiting.length} 项 · 风险 {dynamics.decision_trace.risks.length} 项；无隐藏综合分。</p></article>
          <article className="trend-dynamics-action-next"><span>下一确认信号</span><strong>{detail.next_signal || "补充下一条可观察信号"}</strong></article>
          <article className="trend-dynamics-action-stop"><span>证伪条件</span><strong>{detail.invalidation || "补充证伪条件"}</strong></article>
        </div>
      </div>

      <div className="trend-dynamics-drivers">
        {drivers.map((driver) => <button type="button" key={driver.key} className={`trend-dynamics-driver state-${driver.state}`} onClick={() => setDriverDetail(driver.key as DynamicsDriverKey)}>
          <div><span>{driver.label}</span><Tag>{driver.status}</Tag></div>
          <strong>{driver.metric}</strong>
          <p>{driver.detail}</p>
          <small>点击查看明细 →</small>
        </button>)}
      </div>

      <div className="trend-dynamics-main-grid">
        <section className="trend-dynamics-changes">
          <div className="trend-dynamics-subtitle"><div><strong>最近新增变化</strong><span>信息筛选与官方验证结果</span></div><small>{dynamics.fresh_items.length} 条</small></div>
          {dynamics.fresh_items.length ? dynamics.fresh_items.map((item) => <article key={item.id}>
            <div><Tag color={item.verification_status === "verified" || item.verification_status === "cross_verified" ? "green" : "gold"}>{item.verification_status}</Tag><time>{item.published_at ? item.published_at.slice(5, 16).replace("T", " ") : "时间待定"}</time></div>
            {item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.title}</a> : <strong>{item.title}</strong>}
            <small>{item.source_name} · {item.price_status}</small>
          </article>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="近30日暂无匹配材料" />}
        </section>
      </div>

      <details className="trend-dynamics-history">
        <summary><span>历史因果回放</span><b>{sortedSignals.length} 条记录</b><small>用于验证模型，不占据日常决策第一屏</small></summary>
        <div className="trend-dynamics-stage-strip">{CAUSAL_STAGES.map((stage) => <button key={stage} type="button" onClick={() => onAdd(stage)}><span>{stage}</span><b>{sortedSignals.filter((item) => item.causal_stage === stage).length}</b></button>)}</div>
        {sortedSignals.length ? sortedSignals.map((item) => <article key={item.id} className="trend-dynamics-history-row">
          <time>{item.update_date}</time>
          <div><Tag>{item.causal_stage}</Tag><Tag color={causalStatusColor(item.signal_status)}>{item.signal_status || "线索"}</Tag><strong>{item.content}</strong><small>{item.market_response || item.impact || "尚无盘面验证"}</small></div>
          <Button type="text" size="small" icon={<EditOutlined />} onClick={() => onEdit(item)}>编辑</Button>
        </article>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未建立历史回放" />}
      </details>

      <div className="trend-dynamics-freshness">数据时间：行情 {dynamics.freshness.market || "-"} · 雪球 {dynamics.freshness.xueqiu ? dynamics.freshness.xueqiu.slice(0, 16).replace("T", " ") : "-"} · 信息筛选 {dynamics.freshness.information ? dynamics.freshness.information.slice(0, 16).replace("T", " ") : "-"}</div>
      <DynamicsDetailModal kind={driverDetail} detail={detail} onClose={() => setDriverDetail(null)} />
    </section>
  );
}

export default function IndustryTrendPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const isResearchView = location.pathname.endsWith("/research");
  const queryClient = useQueryClient();
  const params = new URLSearchParams(location.search);
  const parsedId = Number(params.get("trend"));
  const selectedId = Number.isFinite(parsedId) && parsedId > 0 ? parsedId : null;
  const [detailSection, setDetailSection] = useState<"overview" | "causal" | "chain" | "market" | "tracking">("overview");
  const [editing, setEditing] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [generateOpen, setGenerateOpen] = useState<"initial" | "update" | null>(null);
  const [updateCenterOpen, setUpdateCenterOpen] = useState(false);
  const [nodeEditing, setNodeEditing] = useState<IndustryTrendNode | "new" | null>(null);
  const [nodeFormEditing, setNodeFormEditing] = useState(false);
  const [edgeOpen, setEdgeOpen] = useState(false);
  const [companyEditing, setCompanyEditing] = useState<IndustryTrendCompany | "new" | null>(null);
  const [companyViewingId, setCompanyViewingId] = useState<number | null>(null);
  const [companyMarketFilter, setCompanyMarketFilter] = useState<CompanyMarket | "">("");
  const [companyKeyword, setCompanyKeyword] = useState("");
  const [catalystEditing, setCatalystEditing] = useState<IndustryTrendCatalyst | "new" | null>(null);
  const [validationEditing, setValidationEditing] = useState<IndustryTrendValidation | "new" | null>(null);
  const [updateEditing, setUpdateEditing] = useState<IndustryTrendUpdate | "new" | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [draftOpen, setDraftOpen] = useState(false);
  const [decisionOpen, setDecisionOpen] = useState(false);
  const [sourceEditing, setSourceEditing] = useState<IndustryTrendSource | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const blockerConfirmOpen = useRef(false);
  const [coreForm] = Form.useForm();
  const [createForm] = Form.useForm();
  const [generateForm] = Form.useForm();
  const [researchSettingForm] = Form.useForm();
  const [fullUpdateForm] = Form.useForm();
  const [materialForm] = Form.useForm();
  const [decisionForm] = Form.useForm();
  const [sourceForm] = Form.useForm();
  const [nodeForm] = Form.useForm();
  const [edgeForm] = Form.useForm();
  const [companyForm] = Form.useForm();
  const companyMarket = Form.useWatch("market", companyForm) as CompanyMarket | undefined;

  useEffect(() => {
    if (companyMarket && companyMarket !== "A股") companyForm.setFieldValue("is_domestic_alternative", false);
  }, [companyMarket, companyForm]);
  const [catalystForm] = Form.useForm();
  const [validationForm] = Form.useForm();
  const [updateForm] = Form.useForm();

  const list = useQuery({ queryKey: ["industry-trends"], queryFn: () => industryTrendApi.list() });
  const intelligence = useQuery({ queryKey: ["industry-trend-intelligence"], queryFn: () => industryTrendApi.intelligence() });
  const crossMarket = useQuery({ queryKey: ["industry-trend-cross-market"], queryFn: () => industryTrendApi.crossMarketIntelligence(), staleTime: 5 * 60 * 1000, refetchOnWindowFocus: false });
  const crossMarketRefresh = useMutation({
    mutationFn: () => industryTrendApi.crossMarketIntelligence(true),
    onSuccess: (data) => {
      queryClient.setQueryData(["industry-trend-cross-market"], data);
      message.success("跨市场行情已刷新");
    },
    onError: () => message.error("跨市场行情刷新失败，已保留上次结果")
  });
  const detail = useQuery({ queryKey: ["industry-trend", selectedId], queryFn: () => industryTrendApi.get(selectedId!), enabled: Boolean(selectedId && isResearchView) });
  const expectationSummary = useQuery({
    queryKey: ["industry-expectation-summary", selectedId],
    queryFn: () => industryTrendApi.expectationSummary(selectedId!),
    enabled: Boolean(selectedId && isResearchView && detailSection === "market"),
    staleTime: 30 * 60 * 1000,
    retry: false
  });
  const expectationSummaryRefresh = useMutation({
    mutationFn: () => industryTrendApi.refreshExpectationSummary(selectedId!, "missing"),
    onSuccess: (data) => {
      queryClient.setQueryData(["industry-expectation-summary", selectedId], data);
      const result = data.refresh;
      message.success(`板块预期已更新：新增${result.succeeded || 0}家，无覆盖${result.no_consensus || 0}家，失败${result.failed || 0}家`);
    },
    onError: (error) => message.error(`板块预期更新失败：${String(error)}`)
  });
  const versions = useQuery({ queryKey: ["industry-trend-versions", selectedId], queryFn: () => industryTrendApi.versions(selectedId!), enabled: Boolean(selectedId && historyOpen) });
  const researchSettings = useQuery({ queryKey: ["industry-trend-research-settings", selectedId], queryFn: () => industryTrendApi.researchSettings(selectedId!), enabled: Boolean(selectedId && updateCenterOpen) });
  const generationJobs = useQuery({ queryKey: ["industry-trend-jobs", selectedId], queryFn: () => industryTrendApi.jobs(selectedId!), enabled: Boolean(selectedId && updateCenterOpen) });
  const job = useQuery({ queryKey: ["industry-trend-job", jobId], queryFn: () => industryTrendApi.job(jobId!), enabled: Boolean(jobId), refetchInterval: 2000 });

  const chooseTrend = (id: number) => {
    navigate(`/investment/industry-trend/research?trend=${id}`, { replace: isResearchView });
  };

  useEffect(() => {
    if (isResearchView && !selectedId && list.data?.items.length) chooseTrend(list.data.items[0].id);
  }, [list.data, selectedId, isResearchView]);

  useEffect(() => {
    if (!isResearchView && selectedId) navigate(`/investment/industry-trend/research?trend=${selectedId}`, { replace: true });
  }, [isResearchView, selectedId, navigate]);

  useEffect(() => {
    if (!detail.data) return;
    coreForm.setFieldsValue({ ...detail.data, drivers: detail.data.drivers, strength: Math.max(1, Math.min(5, Math.ceil(detail.data.strength / 20))) });
    setDirty(false);
    setEditing(false);
  }, [detail.data, coreForm]);

  useEffect(() => {
    setDetailSection("overview");
  }, [selectedId]);

  useEffect(() => {
    if (!researchSettings.data) return;
    researchSettingForm.setFieldsValue(researchSettings.data);
  }, [researchSettings.data, researchSettingForm]);

  useEffect(() => {
    const current = job.data;
    if (!current) return;
    if (current.status === "succeeded") {
      message.success("Codex研究完成，草稿等待确认");
      setJobId(null);
      queryClient.invalidateQueries({ queryKey: ["industry-trend", current.chain_id] });
      queryClient.invalidateQueries({ queryKey: ["industry-trends"] });
      queryClient.invalidateQueries({ queryKey: ["industry-trend-jobs", current.chain_id] });
      queryClient.invalidateQueries({ queryKey: ["industry-trend-research-settings", current.chain_id] });
      chooseTrend(current.chain_id);
      setUpdateCenterOpen(false);
      setDraftOpen(true);
    } else if (current.status === "failed" || current.status === "cancelled") {
      if (current.status === "failed") message.error(generationErrorText(current.error_message));
      setJobId(null);
    }
  }, [job.data?.status]);

  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);

  const blocker = useBlocker(dirty);
  useEffect(() => {
    if (blocker.state !== "blocked" || blockerConfirmOpen.current) return;
    blockerConfirmOpen.current = true;
    Modal.confirm({
      title: "有未保存修改",
      content: "离开后本次产业逻辑编辑会丢失。",
      okText: "放弃并离开",
      okButtonProps: { danger: true },
      cancelText: "继续编辑",
      onOk: () => blocker.proceed(),
      onCancel: () => blocker.reset(),
      afterClose: () => { blockerConfirmOpen.current = false; }
    });
  }, [blocker]);

  const invalidate = (id = selectedId) => {
    queryClient.invalidateQueries({ queryKey: ["industry-trends"] });
    queryClient.invalidateQueries({ queryKey: ["industry-trend-intelligence"] });
    if (id) queryClient.invalidateQueries({ queryKey: ["industry-trend", id] });
    if (id) queryClient.invalidateQueries({ queryKey: ["industry-expectation-summary", id] });
    queryClient.invalidateQueries({ queryKey: ["industry-trend-versions", id] });
  };

  const action = useMutation({
    mutationFn: (operation: () => Promise<unknown>) => operation(),
    onError: (error) => message.error(String(error))
  });

  const createTrend = async () => {
    const values = await createForm.validateFields();
    const created = await industryTrendApi.create(values);
    message.success("产业已创建");
    setCreateOpen(false);
    createForm.resetFields();
    invalidate(created.id);
    chooseTrend(created.id);
  };

  const saveCore = async () => {
    if (!selectedId) return;
    const values = await coreForm.validateFields();
    await industryTrendApi.update(selectedId, { ...values, strength: Number(values.strength || 1) * 20 });
    message.success("产业研究已保存");
    setEditing(false);
    setDirty(false);
    invalidate();
  };

  const startGeneration = async () => {
    const values = await generateForm.validateFields();
    const payload = { job_type: "initial", ...values, focus_stocks: String(values.focus_stocks || "").split(/[、,，\s]+/).filter(Boolean) };
    const created = await industryTrendApi.generate(payload);
    setGenerateOpen(null);
    generateForm.resetFields();
    setJobId(created.id);
    chooseTrend(created.chain_id);
    message.info("Codex已开始联网研究，可继续浏览页面");
    invalidate(created.chain_id);
  };

  const saveResearchSettings = async () => {
    if (!selectedId) return;
    const values = await researchSettingForm.validateFields();
    const saved = await industryTrendApi.saveResearchSettings(selectedId, { ...values, draft_only: true });
    researchSettingForm.setFieldsValue(saved);
    queryClient.invalidateQueries({ queryKey: ["industry-trend-research-settings", selectedId] });
    message.success("全面更新规则已保存");
  };

  const startFullUpdate = async () => {
    if (!selectedId) return;
    await saveResearchSettings();
    const values = await fullUpdateForm.validateFields();
    const created = await industryTrendApi.generate({ chain_id: selectedId, job_type: "update", ...values });
    fullUpdateForm.resetFields();
    setJobId(created.id);
    message.info("全面更新已开始：覆盖全部节点，只生成变化草稿");
    queryClient.invalidateQueries({ queryKey: ["industry-trend-jobs", selectedId] });
  };

  const saveNode = async () => {
    if (!selectedId || !nodeEditing) return;
    const values = await nodeForm.validateFields();
    if (nodeEditing === "new") await industryTrendApi.createNode(selectedId, values);
    else await industryTrendApi.updateNode(selectedId, nodeEditing.id, values);
    message.success("产业节点已保存");
    setNodeEditing(null);
    setNodeFormEditing(false);
    nodeForm.resetFields();
    invalidate();
  };

  const saveEdge = async () => {
    if (!selectedId) return;
    const values = await edgeForm.validateFields();
    await industryTrendApi.createEdge(selectedId, values);
    message.success("节点连线已新增");
    edgeForm.resetFields();
    invalidate();
  };

  const saveCompany = async () => {
    if (!selectedId || !companyEditing) return;
    const values = await companyForm.validateFields();
    const anchorMarketCapYi = values.expectation_anchor_market_cap_yi;
    delete values.expectation_anchor_market_cap_yi;
    values.expectation_anchor_market_cap = anchorMarketCapYi == null ? null : Number(anchorMarketCapYi) * 100_000_000;
    if (companyEditing === "new") await industryTrendApi.createCompany(selectedId, values);
    else await industryTrendApi.updateCompany(selectedId, companyEditing.id, values);
    message.success("产业股票已保存");
    setCompanyEditing(null);
    companyForm.resetFields();
    invalidate();
  };

  const saveValidation = async () => {
    if (!selectedId || !validationEditing) return;
    const values = await validationForm.validateFields();
    if (validationEditing === "new") await industryTrendApi.createValidation(selectedId, values);
    else await industryTrendApi.updateValidation(selectedId, validationEditing.id, values);
    message.success("验证指标已保存");
    setValidationEditing(null);
    validationForm.resetFields();
    invalidate();
  };

  const saveCatalyst = async () => {
    if (!selectedId || !catalystEditing) return;
    const values = await catalystForm.validateFields();
    if (catalystEditing === "new") await industryTrendApi.createCatalyst(selectedId, values);
    else await industryTrendApi.updateCatalyst(selectedId, catalystEditing.id, values);
    message.success("关键催化事件已保存");
    setCatalystEditing(null);
    catalystForm.resetFields();
    invalidate();
  };

  const saveUpdate = async () => {
    if (!selectedId || !updateEditing) return;
    const values = await updateForm.validateFields();
    if (updateEditing === "new") await industryTrendApi.createUpdate(selectedId, values);
    else await industryTrendApi.updateUpdate(selectedId, updateEditing.id, values);
    message.success("跟踪记录已保存");
    setUpdateEditing(null);
    updateForm.resetFields();
    invalidate();
  };

  const saveMaterial = async () => {
    if (!selectedId) return;
    const values = await materialForm.validateFields();
    await industryTrendApi.createMaterial(selectedId, values);
    materialForm.resetFields();
    materialForm.setFieldsValue({ material_date: new Date().toISOString().slice(0, 10), source_type: "本地对话", change_type: "新增证据" });
    message.success("材料已进入待处理队列");
    invalidate();
  };

  const openDecision = () => {
    if (!selected) return;
    const suggestedCode = policyConsensusDefault(selected.decision_policy);
    decisionForm.setFieldsValue({
      decision_code: suggestedCode,
      primary_company_id: selected.primary_company?.id,
      planned_horizon: 20,
      cost_bps: 20,
      thesis: selected.summary || selected.investment_logic,
      pricing_verdict: [selected.pricing_status, selected.priced_in, selected.not_priced_in].filter(Boolean).join("；"),
      why_best: selected.primary_company?.primary_reason,
      trigger_conditions: selected.next_signal ? [selected.next_signal] : [],
      invalidation_conditions: selected.invalidation ? [selected.invalidation] : []
    });
    setDecisionOpen(true);
  };

  const saveDecision = async () => {
    if (!selectedId) return;
    const values = await decisionForm.validateFields();
    await industryTrendApi.createDecision(selectedId, values);
    setDecisionOpen(false);
    queryClient.invalidateQueries({ queryKey: ["decision-review"] });
    message.success("产业决策已冻结，开始按5/20/60日跟踪");
  };

  const openSourceState = (item: IndustryTrendSource) => {
    sourceForm.setFieldsValue({ evidence_state: item.evidence_state, valid_until: item.valid_until, conflict_note: item.conflict_note });
    setSourceEditing(item);
  };

  const saveSourceState = async () => {
    if (!selectedId || !sourceEditing) return;
    const values = await sourceForm.validateFields();
    await industryTrendApi.updateEvidenceState(selectedId, sourceEditing.id, {
      ...values,
      valid_until: values.valid_until || null,
      conflict_note: values.conflict_note || null
    });
    setSourceEditing(null);
    message.success("证据状态已更新");
    invalidate();
  };

  const openNode = (item: IndustryTrendNode | "new", presetType?: NodeType, editImmediately = false) => {
    nodeForm.resetFields();
    setNodeEditing(item);
    setNodeFormEditing(item === "new" || editImmediately);
    nodeForm.setFieldsValue(item === "new" ? { node_type: presetType || "光互联产品", maturity_status: "验证中", investment_importance: "中", sort_order: (detail.data?.nodes.length ?? 0) * 10 + 10 } : item);
  };

  const closeNode = () => {
    setNodeEditing(null);
    setNodeFormEditing(false);
    nodeForm.resetFields();
  };

  const cancelNodeEdit = () => {
    if (nodeEditing === "new" || !nodeEditing) {
      closeNode();
      return;
    }
    nodeForm.setFieldsValue(nodeEditing);
    setNodeFormEditing(false);
  };

  const openCompany = (item: IndustryTrendCompany | "new") => {
    setCompanyEditing(item);
    companyForm.resetFields();
    companyForm.setFieldsValue(item === "new" ? { market: "A股", tracking_status: "观察", verification_status: "未验证", pricing_status: "部分定价", expectation_gap_status: "无法判断", is_global_leader: false, is_domestic_alternative: false, is_primary: false, sort_order: (detail.data?.companies.length ?? 0) * 10 + 10, node_ids: [] } : {
      ...item,
      expectation_as_of: item.expectation_as_of || new Date().toISOString().slice(0, 10),
      expectation_anchor_market_cap_yi: (item.expectation_anchor_market_cap || item.market_cap_snapshot?.total_market_cap || 0) / 100_000_000
    });
  };

  const openValidation = (item: IndustryTrendValidation | "new") => {
    setValidationEditing(item);
    validationForm.setFieldsValue(item === "new" ? { status: "未验证", priority: "中", sort_order: (detail.data?.validations.length ?? 0) * 10 + 10 } : item);
  };

  const openCatalyst = (item: IndustryTrendCatalyst | "new") => {
    setCatalystEditing(item);
    catalystForm.setFieldsValue(item === "new" ? { status: "预期", importance: "中", event_type: "其他", sort_order: (detail.data?.catalysts.length ?? 0) * 10 + 10 } : item);
  };

  const openUpdate = (item: IndustryTrendUpdate | "new", causalStage?: CausalStage) => {
    setUpdateEditing(item);
    updateForm.setFieldsValue(item === "new" ? {
      update_date: new Date().toISOString().slice(0, 10),
      causal_stage: causalStage,
      evidence_type: causalStage ? "硬事实" : undefined,
      signal_status: causalStage ? "线索" : undefined,
      affects_phase: false,
      affects_decision: false
    } : item);
  };

  const companyColumns = useMemo<ColumnsType<IndustryTrendCompany>>(() => [
    {
      title: "公司",
      width: 185,
      render: (_, row) => {
        const label = <><strong>{row.name}</strong><small className="trend-code">{row.exchange || row.market} · {row.code || "-"}</small></>;
        return <button type="button" className="trend-company-name-button" onClick={() => setCompanyViewingId(row.id)}>{label}</button>;
      }
    },
    { title: "市场", dataIndex: "market", width: 76, render: (value) => <Tag color={value === "A股" ? "red" : value === "美股" ? "blue" : "cyan"}>{value}</Tag> },
    { title: "产业位置", dataIndex: "position", width: 130, render: (value) => value || "-" },
    { title: "公司地位", dataIndex: "company_standing", width: 130, render: (value) => value || "-" },
    { title: "核心优势", dataIndex: "core_advantage", ellipsis: true, render: (value) => value || "-" },
    {
      title: "盘面确认",
      width: 150,
      render: (_, row) => row.market_snapshot ? (
        <div className="trend-market-cell">
          <strong>{row.market_snapshot.close.toFixed(2)} <span className={(row.market_snapshot.change_pct ?? 0) >= 0 ? "market-up" : "market-down"}>{formatPercent(row.market_snapshot.change_pct)}</span></strong>
          <small>20日 {formatPercent(row.market_snapshot.return_20d_pct)} · {formatAmount(row.market_snapshot.amount)}</small>
        </div>
      ) : <span className="decision-pending">暂无本地行情</span>
    },
    { title: "受益", dataIndex: "benefit_directness", width: 75, render: (value) => value ? <Tag color="cyan">{value}</Tag> : "-" },
    { title: "验证", dataIndex: "verification_status", width: 90, render: (value) => <Tag>{value}</Tag> },
    { title: "关注", dataIndex: "tracking_status", width: 100, render: (value) => <Tag color={value === "核心受益" ? "gold" : undefined}>{value}</Tag> },
    { title: "定价", dataIndex: "pricing_status", width: 90, render: (value) => <Tag color={pricingColor(value)}>{value}</Tag> },
    { title: "预期差", dataIndex: "expectation_gap_status", width: 110, render: (value: ExpectationGapStatus) => <Tag color={expectationGapColor(value)}>{value || "无法判断"}</Tag> },
    { title: "推荐逻辑", width: 150, render: (_, row) => <Space size={[3, 3]} wrap>{row.is_global_leader ? <Tag color="purple">全球绝对优势</Tag> : null}{row.is_domestic_alternative ? <Tag color="cyan">国内可替代</Tag> : null}{!row.is_global_leader && !row.is_domestic_alternative ? "-" : null}</Space> },
    { title: "交易首选", dataIndex: "is_primary", width: 76, render: (value) => value ? <StarFilled className="primary-star" /> : "-" },
    { title: "操作", width: 88, fixed: "right", render: (_, row) => <Space size={0}><Button type="text" size="small" icon={<EditOutlined />} onClick={() => openCompany(row)} /><Popconfirm title="删除该产业股票？" onConfirm={() => selectedId && action.mutateAsync(() => industryTrendApi.deleteCompany(selectedId, row.id)).then(() => invalidate())}><Button type="text" danger size="small" icon={<DeleteOutlined />} /></Popconfirm></Space> }
  ], [selectedId, detail.data]);

  const selected = detail.data;
  const viewedCompany = companyViewingId ? selected?.companies.find((company) => company.id === companyViewingId) ?? null : null;
  const companyBeingEdited = companyEditing && companyEditing !== "new" ? companyEditing : null;
  const activeNode = nodeEditing && nodeEditing !== "new" ? nodeEditing : null;
  const activeNodeTypeMeta = activeNode ? nodeTypeMeta(selected?.name || "", activeNode.node_type) : null;
  const activeNodeCompanies = activeNode ? (selected?.companies ?? []).filter((company) => company.node_ids.includes(activeNode.id)) : [];
  const activeNodeValidations = activeNode ? (selected?.validations ?? []).filter((validation) => validation.node_id === activeNode.id) : [];
  const activeNodeSources = activeNode ? (selected?.sources ?? []).filter((source) => source.node_ids.includes(activeNode.id)) : [];
  const filteredCompanies = useMemo(() => (selected?.companies ?? []).filter((company) => {
    if (companyMarketFilter && company.market !== companyMarketFilter) return false;
    const query = companyKeyword.trim().toLowerCase();
    if (!query) return true;
    return [company.name, company.code, company.position, company.company_standing]
      .some((value) => String(value || "").toLowerCase().includes(query));
  }), [selected?.companies, companyMarketFilter, companyKeyword]);
  const companyMarketCounts = useMemo(() => COMPANY_MARKETS.map((market) => ({
    market,
    count: (selected?.companies ?? []).filter((company) => company.market === market).length
  })).filter((item) => item.count > 0), [selected?.companies]);
  const nextCatalyst = selected?.catalysts.find((item) => item.status !== "兑现") ?? selected?.catalysts[0] ?? null;
  const activeJob = job.data && ["queued", "running"].includes(job.data.status) ? job.data : null;

  return (
    <main className="industry-trend-page">
      <header className="industry-trend-page-header">
        <div>
          <Typography.Title level={2}>{isResearchView ? "产业研究" : "产业总览"}</Typography.Title>
          <Typography.Text>{isResearchView ? "完成判断、拆链和验证。" : "比较方向后进入单个产业研究。"}</Typography.Text>
        </div>
        <Space wrap>
          {!isResearchView ? <><Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建产业</Button><Button type="primary" icon={<RobotOutlined />} onClick={() => { setGenerateOpen("initial"); generateForm.setFieldsValue({}); }}>首次研究</Button></> : null}
          {isResearchView && selected ? <Button type="primary" icon={<SaveOutlined />} onClick={openDecision}>形成决策</Button> : null}
          {isResearchView && selected ? <Button icon={<SettingOutlined />} onClick={() => setUpdateCenterOpen(true)}>更新中心</Button> : null}
          {isResearchView && selected ? <Button icon={<HistoryOutlined />} onClick={() => setHistoryOpen(true)}>历史</Button> : null}
          {isResearchView && selected && !editing ? <Button type="primary" icon={<EditOutlined />} onClick={() => { setDetailSection("overview"); setEditing(true); }}>编辑产业判断</Button> : null}
          {isResearchView && selected && editing ? <><Button onClick={() => { coreForm.setFieldsValue({ ...selected, drivers: selected.drivers, strength: Math.max(1, Math.min(5, Math.ceil(selected.strength / 20))) }); setEditing(false); setDirty(false); }}>取消</Button><Button type="primary" icon={<SaveOutlined />} onClick={saveCore}>保存</Button></> : null}
        </Space>
      </header>

      {activeJob ? (
        <Alert className="trend-job-alert" type="info" showIcon icon={<RobotOutlined />} message={`Codex正在研究：${activeJob.phase}`} description={<Space><Progress percent={activeJob.status === "queued" ? 15 : 55} status="active" showInfo={false} /><Button size="small" danger onClick={() => industryTrendApi.cancelJob(activeJob.id).then(() => setJobId(null))}>取消</Button></Space>} />
      ) : null}

      {!isResearchView ? <AIIndustryIntelligence data={intelligence.data} crossMarket={crossMarket.data} loading={intelligence.isLoading} crossMarketLoading={crossMarket.isLoading} crossMarketRefreshing={crossMarketRefresh.isPending} onRefreshCrossMarket={() => crossMarketRefresh.mutate()} onSelect={chooseTrend} /> : null}

      {isResearchView ? <>
      <div className="trend-research-switcher">
        <Button onClick={() => navigate("/investment/industry-trend")}>← 返回产业总览</Button>
        <Select showSearch optionFilterProp="label" value={selectedId || undefined} placeholder="选择产业" options={(list.data?.items ?? []).map((item) => ({ value: item.id, label: item.name }))} onChange={chooseTrend} />
        <Typography.Text>{selected ? `${selected.phase} · ${selected.attention_level} · 第${selected.revision}版` : "选择需要研究的产业"}</Typography.Text>
        <Space className="trend-research-actions"><Button icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>新建</Button><Button icon={<RobotOutlined />} onClick={() => { setGenerateOpen("initial"); generateForm.setFieldsValue({}); }}>首次研究</Button></Space>
      </div>

      <div className="industry-trend-workbench is-research-only">

        <section className="trend-detail-panel">
          {detail.isLoading ? <div className="trend-detail-empty">产业研究加载中...</div> : !selected ? (
            <Empty description="从左侧选择产业，或新建一项研究" />
          ) : (
            <>
              {selected.draft ? <Alert className="trend-draft-alert" type="warning" showIcon message="Codex草稿等待确认" description={`来源：${selected.draft.source} · ${formatTime(selected.draft.updated_at)}`} action={<Space><Button size="small" onClick={() => setDraftOpen(true)}>查看草稿</Button><Button size="small" type="primary" onClick={() => action.mutateAsync(() => industryTrendApi.applyDraft(selected.id)).then(() => { message.success("草稿已应用并生成历史版本"); invalidate(); })}>应用草稿</Button></Space>} /> : null}
              {selected.validations.some((item) => item.status === "失败") ? <Alert className="trend-draft-alert" type="error" showIcon message="存在验证失败项，正式判断尚未自动修改" description="请复核失败原因，再人工确认是否将方向降为观察或否决。" action={<Button size="small" onClick={() => { setDetailSection("overview"); setEditing(true); }}>调整判断</Button>} /> : null}
              <section className="trend-decision-hero">
                <div className="trend-decision-heading">
                  <div><Space wrap><Typography.Title level={2}>{selected.name}</Typography.Title><Tag color="blue">{selected.phase}</Tag><Tag>{selected.attention_level}</Tag><TrendStars strength={selected.strength} /></Space><p>{selected.summary || "尚未形成一句话产业判断"}</p></div>
                  <div className="trend-overall"><span>总体判断</span><strong className={`verdict-${selected.overall_verdict}`}>{selected.overall_verdict}</strong><small>第 {selected.revision} 版</small></div>
                </div>
                <div className="trend-current-status">
                  <div><span>产业阶段</span><strong>{selected.phase}</strong></div>
                  <div><span>投资状态</span><strong>{selected.attention_level}</strong></div>
                  <div><span>趋势强度</span><TrendStars strength={selected.strength} /></div>
                  <div><span>下一关键事件</span><strong>{nextCatalyst ? `${nextCatalyst.expected_time || "时间待定"} · ${nextCatalyst.event_name}` : "尚未设置"}</strong></div>
                </div>
                <div className="trend-verdict-grid">
                  <VerdictCard label="方向判断" value={selected.direction_verdict} note={selected.change_summary} />
                  <VerdictCard label="股票判断" value={selected.stock_verdict} note={`全球优势代表（按环节） ${companyRepresentativeSummary(selected.company_recommendations.global_leaders, 5)} · 国内替代 ${companyRepresentativeSummary(selected.company_recommendations.domestic_alternatives, 3)}`} />
                  <VerdictCard label="时点判断" value={selected.timing_verdict} note={selected.pricing_status} />
                </div>
                <div className="trend-trigger-strip"><div><span>下一条确认信号</span><strong>{selected.next_signal || "未填写"}</strong></div><div><span>主要证伪条件</span><strong>{selected.invalidation || "未填写"}</strong></div></div>
              </section>
              <DecisionPolicyPanel evaluation={selected.decision_policy} />

              <nav className="trend-detail-navigation" aria-label="产业研究模块">
                <Segmented
                  block
                  value={detailSection}
                  onChange={(value) => setDetailSection(value as "overview" | "causal" | "chain" | "market" | "tracking")}
                  options={[
                    { label: "决策总览", value: "overview" },
                    { label: `上涨动力 ${selected.updates.filter((item) => item.causal_stage).length}`, value: "causal" },
                    { label: `产业链 ${selected.nodes.length}`, value: "chain" },
                    { label: `公司与事件 ${selected.companies.length}/${selected.catalysts.length}`, value: "market" },
                    { label: `验证与记录 ${selected.validations.length}/${selected.updates.length}`, value: "tracking" }
                  ]}
                />
              </nav>

              {detailSection === "overview" ? <>
              <section className="trend-terminal-section">
                <div className="trend-section-title"><div><Typography.Title level={4}>产业逻辑</Typography.Title><Typography.Text>先判断变化与定价，再展开产业链</Typography.Text></div>{editing && dirty ? <Tag color="orange">未保存</Tag> : null}</div>
                {editing ? (
                  <Form form={coreForm} layout="vertical" onValuesChange={() => setDirty(true)} className="trend-core-form">
                    <Form.Item name="name" label="产业名称" rules={[{ required: true }]}><Input /></Form.Item>
                    <Form.Item name="summary" label="一句话产业判断"><TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
                    <Form.Item name="change_summary" label="产业发生了什么变化"><TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
                    <Form.Item name="why_now" label="为什么是现在"><TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
                    <Form.Item name="investment_logic" label="核心投资逻辑"><TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
                    <Form.Item name="expected_duration" label="预期持续周期"><Input /></Form.Item>
                    <Form.Item name="drivers" label="核心驱动"><Select mode="tags" options={DRIVERS.map((value) => ({ value }))} /></Form.Item>
                    <Form.Item name="phase" label="产业阶段"><Select options={PHASES.map((value) => ({ value }))} /></Form.Item>
                    <Form.Item name="strength" label="趋势强度（1-5星）"><InputNumber min={1} max={5} /></Form.Item>
                    <Form.Item name="attention_level" label="关注等级"><Select options={ATTENTION_LEVELS.map((value) => ({ value }))} /></Form.Item>
                    <Form.Item name="direction_verdict" label="方向判断"><Select options={VERDICTS.map((value) => ({ value }))} /></Form.Item>
                    <Form.Item name="stock_verdict" label="股票判断"><Select options={VERDICTS.map((value) => ({ value }))} /></Form.Item>
                    <Form.Item name="pricing_status" label="市场定价"><Select options={PRICING_STATUSES.map((value) => ({ value }))} /></Form.Item>
                    <Form.Item name="timing_verdict" label="时点判断"><Select options={VERDICTS.map((value) => ({ value }))} /></Form.Item>
                    <Form.Item name="priced_in" label="已经被市场反映"><TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
                    <Form.Item name="not_priced_in" label="尚未被市场反映"><TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
                    <Form.Item name="next_signal" label="下一条确认信号"><TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
                    <Form.Item name="invalidation" label="主要证伪条件"><TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
                    <Form.Item name="risk" label="主要风险"><TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
                  </Form>
                ) : <LogicView detail={selected} />}
              </section>

              <section className="trend-terminal-section trend-lifecycle-section">
                <div className="trend-section-title"><div><Typography.Title level={4}>产业生命周期</Typography.Title><Typography.Text>进入当前阶段 {selected.phase_entered_at || "-"}</Typography.Text></div></div>
                <Steps current={Math.max(0, PHASES.indexOf(selected.phase))} items={PHASES.map((title) => ({ title }))} responsive={false} />
              </section>
              </> : null}

              {detailSection === "causal" ? <>
                <CrossMarketSectorPanel sector={crossMarket.data?.sectors.find((item) => item.id === selected.id)} data={crossMarket.data} refreshing={crossMarketRefresh.isPending} onRefresh={() => crossMarketRefresh.mutate()} />
                <CausalReplay detail={selected} onAdd={(stage) => openUpdate("new", stage)} onEdit={(item) => openUpdate(item)} />
              </> : null}

              {detailSection === "chain" ? <NodeMap
                detail={selected}
                onView={(item) => openNode(item)}
                onViewCompany={(company) => setCompanyViewingId(company.id)}
                onEdit={(item) => openNode(item, undefined, true)}
                onDelete={(item) => Modal.confirm({ title: `删除节点「${item.name}」？`, okText: "删除", okButtonProps: { danger: true }, onOk: () => action.mutateAsync(() => industryTrendApi.deleteNode(selected.id, item.id)).then(() => invalidate()) })}
                onAdd={(type) => openNode("new", type)}
                onEdges={() => setEdgeOpen(true)}
                onReorder={(from, to) => Promise.all([industryTrendApi.updateNode(selected.id, from.id, { sort_order: to.sort_order }), industryTrendApi.updateNode(selected.id, to.id, { sort_order: from.sort_order })]).then(() => invalidate())}
              /> : null}

              {detailSection === "market" ? <>
              {expectationSummary.isError ? <Alert type="warning" showIcon message="板块一致预期读取失败" description={String(expectationSummary.error)} /> : null}
              <IndustryExpectationPanel
                data={expectationSummary.data}
                loading={expectationSummary.isLoading}
                refreshing={expectationSummaryRefresh.isPending}
                onRefresh={() => expectationSummaryRefresh.mutate()}
                onCompany={(companyId) => setCompanyViewingId(companyId)}
              />
              <section className="trend-terminal-section">
                <div className="trend-section-title"><div><Typography.Title level={4}>产业公司池</Typography.Title><Typography.Text>先标全球绝对优势，再找国内可替代；A股交易首选单独判断</Typography.Text></div><Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => openCompany("new")}>新增公司</Button></div>
                <div className="trend-company-recommendation-pairs">
                  <section>
                    <header><Tag color="purple">1</Tag><div><strong>全球优势代表（按细分环节）</strong><span>代表而非唯一；按模块、芯片、器件、设备与光纤连接等环节同时比较</span></div></header>
                    <div>{selected.company_recommendations.global_leaders.length ? selected.company_recommendations.global_leaders.map((item) => <button type="button" key={item.id} onClick={() => setCompanyViewingId(item.id)}><b>{item.name}</b><span>{item.market} · {item.position || "环节待标注"} · {item.verification_status}</span><small>{item.reason || "优势依据待补充"}</small></button>) : <p>尚未标注。不能只凭“龙头”称号，需要硬证据。</p>}</div>
                  </section>
                  <section>
                    <header><Tag color="cyan">2</Tag><div><strong>国内可替代公司</strong><span>明确替代海外环节，并跟踪认证、订单和量产</span></div></header>
                    <div>{selected.company_recommendations.domestic_alternatives.length ? selected.company_recommendations.domestic_alternatives.map((item) => <button type="button" key={item.id} onClick={() => setCompanyViewingId(item.id)}><b>{item.name}</b><span>{item.market} · {item.verification_status}</span><small>{item.reason || "替代路径待补充"}</small></button>) : <p>尚未标注。需要说明替代对象和当前验证进度。</p>}</div>
                  </section>
                </div>
                <div className="trend-company-toolbar">
                  <Space wrap>{companyMarketCounts.map((item) => <Button key={item.market} size="small" type={companyMarketFilter === item.market ? "primary" : "default"} onClick={() => setCompanyMarketFilter(companyMarketFilter === item.market ? "" : item.market)}>{item.market} {item.count}</Button>)}</Space>
                  <Input allowClear value={companyKeyword} onChange={(event) => setCompanyKeyword(event.target.value)} placeholder="搜索公司或产业位置" />
                </div>
                <Table rowKey="id" size="small" scroll={{ x: 1570 }} columns={companyColumns} dataSource={filteredCompanies} pagination={{ pageSize: 15, hideOnSinglePage: true }} locale={{ emptyText: "没有符合条件的产业公司" }} />
              </section>

              <section className="trend-terminal-section">
                <div className="trend-section-title"><div><Typography.Title level={4}>关键事件催化</Typography.Title><Typography.Text>什么事情会推动产业进入下一阶段</Typography.Text></div><Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => openCatalyst("new")}>新增事件</Button></div>
                <div className="trend-catalyst-stages">{CATALYST_STATUSES.map((status, index) => <div className="trend-catalyst-stage" key={status}><span>{index + 1}</span><strong>{status}</strong>{index < CATALYST_STATUSES.length - 1 ? <ArrowRightOutlined /> : null}</div>)}</div>
                <div className="trend-catalyst-list">
                  {selected.catalysts.length ? selected.catalysts.map((item) => {
                    const node = selected.nodes.find((entry) => entry.id === item.impact_node_id);
                    const company = selected.companies.find((entry) => entry.id === item.impact_company_id);
                    return (
                      <article className={`trend-catalyst-card status-${item.status}`} key={item.id}>
                        <div className="trend-catalyst-card-top"><span><CalendarOutlined /> {item.expected_time || "时间待定"}</span><Space size={5}><Tag color={item.importance === "高" ? "red" : item.importance === "中" ? "gold" : undefined}>{item.importance}重要</Tag><Tag color={catalystStatusColor(item.status)}>{item.status}</Tag></Space></div>
                        <strong>{item.event_name}</strong>
                        <p>{item.impact || "尚未填写预期影响"}</p>
                        <div className="trend-catalyst-meta"><span>{item.event_type}</span>{node ? <Tag>{node.name}</Tag> : null}{company ? <Tag>{company.name}</Tag> : null}{item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.source_name || "来源"}</a> : null}<Space size={0}><Button type="text" size="small" icon={<EditOutlined />} onClick={() => openCatalyst(item)} /><Popconfirm title="删除该催化事件？" onConfirm={() => action.mutateAsync(() => industryTrendApi.deleteCatalyst(selected.id, item.id)).then(() => invalidate())}><Button type="text" danger size="small" icon={<DeleteOutlined />} /></Popconfirm></Space></div>
                      </article>
                    );
                  }) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无关键催化事件" />}
                </div>
              </section>
              </> : null}

              {detailSection === "tracking" ? <>
              <section className="trend-terminal-section">
                <div className="trend-section-title"><div><Typography.Title level={4}>投资验证路线图</Typography.Title><Typography.Text>沿产业传导逐步确认：逻辑 → 需求 → 订单 → 利润</Typography.Text></div><Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => openValidation("new")}>新增验证</Button></div>
                <div className="trend-validation-summary"><span>{selected.validations.filter((item) => item.status === "已确认").length}/{selected.validations.length} 已确认</span><span>{selected.validations.filter((item) => item.status === "验证中").length} 验证中</span><span>{selected.validations.filter((item) => item.status === "失败").length} 失败</span></div>
                <div className="trend-validation-route">
                  {selected.validations.length ? selected.validations.map((item) => (
                    <div className={`trend-validation-step status-${item.status}`} key={item.id}>
                      <div className="trend-validation-index">{selected.validations.indexOf(item) + 1}</div>
                      <Checkbox checked={item.status === "已确认"} onChange={(event) => industryTrendApi.updateValidation(selected.id, item.id, { status: event.target.checked ? "已确认" : "验证中" }).then(() => invalidate())} />
                      <div className="trend-validation-content"><strong>{item.name}</strong><p>{item.criteria || "未设置验证标准"}</p><small>{item.current_result || "暂无当前结果"}{item.target_date ? ` · 目标 ${item.target_date}` : ""}</small><Space size={4} wrap>{item.node_id ? <Tag>{selected.nodes.find((node) => node.id === item.node_id)?.name}</Tag> : null}{item.company_id ? <Tag>{selected.companies.find((company) => company.id === item.company_id)?.name}</Tag> : null}</Space></div>
                      <Tag>{item.status}</Tag>
                      <Space size={0}><Button type="text" size="small" icon={<EditOutlined />} onClick={() => openValidation(item)} /><Popconfirm title="删除该验证指标？" onConfirm={() => action.mutateAsync(() => industryTrendApi.deleteValidation(selected.id, item.id)).then(() => invalidate())}><Button type="text" danger size="small" icon={<DeleteOutlined />} /></Popconfirm></Space>
                    </div>
                  )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无验证路线，按产业传导顺序新增验证" />}
                </div>
              </section>

              <section className="trend-terminal-section">
                <div className="trend-section-title"><div><Typography.Title level={4}>跟踪时间轴</Typography.Title><Typography.Text>记录变化、影响和下一步验证</Typography.Text></div><Button size="small" type="primary" icon={<PlusOutlined />} onClick={() => openUpdate("new")}>新增记录</Button></div>
                {selected.updates.some((item) => !item.causal_stage) ? <Timeline items={selected.updates.filter((item) => !item.causal_stage).map((item) => ({ color: item.affects_decision ? "orange" : "blue", children: <div className="trend-timeline-item"><div><strong>{item.update_date} · {item.content}</strong><Space size={0}><Button type="text" size="small" icon={<EditOutlined />} onClick={() => openUpdate(item)} /><Popconfirm title="删除该跟踪记录？" onConfirm={() => action.mutateAsync(() => industryTrendApi.deleteUpdate(selected.id, item.id)).then(() => invalidate())}><Button type="text" danger size="small" icon={<DeleteOutlined />} /></Popconfirm></Space></div><p>{item.impact || "未填写影响"}</p><small>下一步：{item.next_verification || "未填写"}{item.source_url ? <> · <a href={item.source_url} target="_blank" rel="noreferrer">{item.source_name || "来源"}</a></> : ""}</small></div> }))} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无普通跟踪记录；上涨因果记录请到“上涨因果”查看" />}
              </section>

              {selected.sources.length ? <section className="trend-terminal-section"><div className="trend-section-title"><div><Typography.Title level={4}>关键来源</Typography.Title><Typography.Text>官方事实、市场线索和互证状态分开呈现</Typography.Text></div></div><List size="small" dataSource={selected.sources} renderItem={(item) => {
                const sourceNodeNames = item.node_ids.map((nodeId) => selected.nodes.find((node) => node.id === nodeId)?.name).filter(Boolean);
                return <List.Item actions={[<Button key="state" size="small" onClick={() => openSourceState(item)}>状态</Button>]}><List.Item.Meta title={<Space wrap size={5}>{item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.title}</a> : item.title}<Tag color={item.source_tier === "雪球线索" || item.source_tier === "市场数据" ? "blue" : "green"}>{item.source_tier}</Tag><Tag color={item.verification_status === "已互证" ? "green" : item.verification_status === "市场线索" ? "blue" : "gold"}>{item.verification_status}</Tag><Tag color={item.evidence_state === "存在冲突" ? "red" : item.evidence_state === "已失效" ? "default" : item.evidence_state === "待复核" ? "orange" : "green"}>{item.evidence_state}</Tag></Space>} description={<span>{item.evidence_date} · {item.source_name || "未注明来源"} · 影响{item.impact_level}{sourceNodeNames.length ? ` · 关联：${sourceNodeNames.join("、")}` : " · 尚未绑定节点"}{item.valid_until ? ` · 有效至 ${item.valid_until}` : ""}{item.conflict_note ? ` · ${item.conflict_note}` : ""}</span>} /></List.Item>;
              }} /></section> : null}
              </> : null}
            </>
          )}
        </section>
      </div>
      </> : null}

      <Modal open={createOpen} title="手工新建产业" okText="创建" cancelText="取消" confirmLoading={action.isPending} onOk={() => action.mutateAsync(createTrend)} onCancel={() => setCreateOpen(false)}><Form form={createForm} layout="vertical" initialValues={{ attention_level: "观察" }}><Form.Item name="name" label="产业名称" rules={[{ required: true }]}><Input autoFocus /></Form.Item><Form.Item name="summary" label="一句话线索"><TextArea rows={3} /></Form.Item><Form.Item name="attention_level" label="关注等级"><Select options={ATTENTION_LEVELS.map((value) => ({ value }))} /></Form.Item></Form></Modal>

      <Modal open={decisionOpen} title="形成产业投资决策" okText="冻结并开始跟踪" cancelText="取消" confirmLoading={action.isPending} onOk={() => action.mutateAsync(saveDecision)} onCancel={() => setDecisionOpen(false)} width={720}>
        <Alert type="info" showIcon message="人工判断会单独冻结，不会被策略建议覆盖" description="qq 2.1与简单基线都是待验证策略。两者分歧时默认先放C类，你可以依据当时证据人工改判。" />
        <DecisionPolicyPanel evaluation={selected?.decision_policy} />
        <Form form={decisionForm} layout="vertical" className="trend-decision-form">
          <Form.Item name="decision_code" label="人工冻结判断" rules={[{ required: true }]}><Select options={[{ value: "A", label: "A｜进入模拟交易计划" }, { value: "B", label: "B｜等价格或催化" }, { value: "C", label: "C｜等待验证" }, { value: "D", label: "D｜否决或过滤" }]} /></Form.Item>
          <Form.Item name="primary_company_id" label="主选A股"><Select allowClear options={(selected?.companies ?? []).filter((item) => item.market === "A股").map((item) => ({ value: item.id, label: `${item.name} · ${item.full_code || item.code}` }))} /></Form.Item>
          <Form.Item name="planned_horizon" label="主要复盘周期"><Select options={[5, 20, 60].map((value) => ({ value, label: `${value}个交易日` }))} /></Form.Item>
          <Form.Item name="cost_bps" label="模拟成本（bp）"><InputNumber min={0} max={200} /></Form.Item>
          <Form.Item name="thesis" label="当时核心判断" className="is-wide" rules={[{ required: true }]}><TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
          <Form.Item name="pricing_verdict" label="市场已反映 / 尚未反映" className="is-wide" rules={[{ required: true }]}><TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
          <Form.Item name="why_best" label="为什么主选优于其他候选" className="is-wide"><TextArea autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item>
          <Form.Item name="trigger_conditions" label="下一确认信号" className="is-wide"><Select mode="tags" tokenSeparators={["；", ";"]} /></Form.Item>
          <Form.Item name="invalidation_conditions" label="证伪条件" className="is-wide"><Select mode="tags" tokenSeparators={["；", ";"]} /></Form.Item>
        </Form>
      </Modal>

      <Modal open={Boolean(sourceEditing)} title="证据有效性" okText="保存状态" cancelText="取消" confirmLoading={action.isPending} onOk={() => action.mutateAsync(saveSourceState)} onCancel={() => setSourceEditing(null)}>
        <Form form={sourceForm} layout="vertical">
          <Form.Item name="evidence_state" label="当前状态" rules={[{ required: true }]}><Select options={["有效", "待复核", "已失效", "存在冲突"].map((value) => ({ value }))} /></Form.Item>
          <Form.Item name="valid_until" label="有效期"><Input type="date" /></Form.Item>
          <Form.Item name="conflict_note" label="失效或冲突说明"><TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
        </Form>
      </Modal>

      <Modal open={Boolean(generateOpen)} title="Codex首次研究" okText="开始联网研究" cancelText="取消" onOk={() => action.mutateAsync(startGeneration)} onCancel={() => setGenerateOpen(null)} confirmLoading={action.isPending} width={620}><Alert type="info" showIcon message="生成结果只进入草稿，确认后才更新正式研究。" /><Form form={generateForm} layout="vertical"><Form.Item name="name" label="产业名称" rules={[{ required: true }]}><Input autoFocus placeholder="例如：AI基础设施" /></Form.Item><Form.Item name="focus_stocks" label="可选关注股票"><Input placeholder="代码或名称，用逗号分隔" /></Form.Item><Form.Item name="clue" label="研究线索"><TextArea rows={4} placeholder="最近出现了什么变化，或希望重点验证什么" /></Form.Item><Form.Item name="requirements" label="补充要求"><TextArea rows={3} /></Form.Item></Form></Modal>

      <Drawer className="trend-update-center" open={updateCenterOpen} onClose={() => setUpdateCenterOpen(false)} width={760} title="产业全面更新中心">
        <Alert type="info" showIcon message="只保留全面更新模式" description="覆盖全部产业节点与验证维度；Codex只输出新增、失效、冲突和证据缺口，确认时按差异合并，不重建旧资料。" />
        <section className="trend-update-center-section trend-material-queue">
          <div className="trend-update-center-heading"><div><Typography.Title level={4}>新材料队列</Typography.Title><Typography.Text>本地对话、公告、雪球和手工线索先进入这里，全面更新时自动带入。</Typography.Text></div><Tag color={(selected?.materials ?? []).some((item) => item.status === "待处理") ? "orange" : "default"}>{(selected?.materials ?? []).filter((item) => item.status === "待处理").length} 条待处理</Tag></div>
          <Form form={materialForm} layout="vertical" initialValues={{ material_date: new Date().toISOString().slice(0, 10), source_type: "本地对话", change_type: "新增证据" }} className="trend-material-form">
            <Form.Item name="title" label="材料标题" rules={[{ required: true }]}><Input placeholder="一句话写清新增变化" /></Form.Item>
            <Form.Item name="material_date" label="日期" rules={[{ required: true }]}><Input type="date" /></Form.Item>
            <Form.Item name="source_type" label="来源类型"><Select options={["本地对话", "公告/财报", "公司官网", "雪球线索", "同花顺", "其他"].map((value) => ({ value }))} /></Form.Item>
            <Form.Item name="change_type" label="变化类型"><Select options={["新增证据", "信息冲突", "证据失效", "待验证"].map((value) => ({ value }))} /></Form.Item>
            <Form.Item name="source_name" label="来源名称"><Input placeholder="例如：巨潮资讯、雪球用户、Codex对话" /></Form.Item>
            <Form.Item name="source_url" label="来源链接"><Input placeholder="可不填" /></Form.Item>
            <Form.Item name="content" label="材料内容" className="is-wide"><TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
            <Form.Item className="is-wide trend-material-submit"><Button type="primary" icon={<PlusOutlined />} onClick={() => action.mutateAsync(saveMaterial)}>加入队列</Button></Form.Item>
          </Form>
          <List
            size="small"
            dataSource={selected?.materials ?? []}
            locale={{ emptyText: "暂无更新材料" }}
            renderItem={(item) => <List.Item actions={[
              item.status === "待处理" ? <Button key="ignore" size="small" onClick={() => industryTrendApi.updateMaterial(item.chain_id, item.id, { status: "忽略" }).then(() => invalidate())}>忽略</Button> : <Button key="retry" size="small" onClick={() => industryTrendApi.updateMaterial(item.chain_id, item.id, { status: "待处理" }).then(() => invalidate())}>重新处理</Button>,
              <Popconfirm key="delete" title="删除这条材料？" onConfirm={() => industryTrendApi.deleteMaterial(item.chain_id, item.id).then(() => invalidate())}><Button type="text" danger size="small" icon={<DeleteOutlined />} /></Popconfirm>
            ]}><List.Item.Meta title={<Space wrap><strong>{item.title}</strong><Tag color={item.status === "待处理" ? "orange" : item.status === "已纳入" ? "green" : undefined}>{item.status}</Tag><Tag>{item.change_type}</Tag></Space>} description={`${item.material_date} · ${item.source_name || item.source_type}${item.content ? ` · ${item.content}` : ""}`} /></List.Item>}
          />
        </section>
        <section className="trend-update-center-section">
          <div className="trend-update-center-heading"><div><Typography.Title level={4}>长期研究设定</Typography.Title><Typography.Text>网页保存固定规则；对话里的临时要求只影响本次任务。</Typography.Text></div><Button icon={<SaveOutlined />} onClick={() => action.mutateAsync(saveResearchSettings)}>保存设定</Button></div>
          <Form form={researchSettingForm} layout="vertical" initialValues={{ source_preferences: RESEARCH_SOURCE_PREFERENCES, token_budget: 24000, allow_new_nodes: true, allow_new_companies: true, draft_only: true }}>
            <Form.Item name="priority_nodes" label="重点产业节点"><Select mode="multiple" allowClear placeholder="重点关注但不会跳过其他节点" options={(selected?.nodes ?? []).map((node) => ({ label: node.name, value: node.name }))} /></Form.Item>
            <Form.Item name="priority_companies" label="重点公司"><Select mode="multiple" allowClear placeholder="产业内外公司均可" options={(selected?.companies ?? []).map((company) => ({ label: `${company.name} · ${company.market}`, value: company.name }))} /></Form.Item>
            <Form.Item name="source_preferences" label="默认信息源"><Checkbox.Group options={RESEARCH_SOURCE_PREFERENCES} /></Form.Item>
            <Form.Item name="excluded_keywords" label="排除关键词"><Select mode="tags" tokenSeparators={[",", "，", "、"]} placeholder="例如：纯营销稿、无来源转载" /></Form.Item>
            <Form.Item name="evidence_rules" label="证据互证规则"><TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
            <Form.Item name="custom_instructions" label="长期补充要求"><TextArea autoSize={{ minRows: 2, maxRows: 5 }} placeholder="例如：海外公司用于技术坐标，A股用于投资表达" /></Form.Item>
            <Space wrap align="start">
              <Form.Item name="token_budget" label="单次Token目标" rules={[{ required: true }]}><InputNumber min={4000} max={100000} step={2000} /></Form.Item>
              <Form.Item name="allow_new_nodes" valuePropName="checked" label="结构扩展"><Checkbox>允许建议新增产业节点</Checkbox></Form.Item>
              <Form.Item name="allow_new_companies" valuePropName="checked" label="公司扩展"><Checkbox>允许建议新增全球公司</Checkbox></Form.Item>
            </Space>
            <Alert type="warning" showIcon message="固定只生成草稿" description="产业阶段、双线公司推荐、A股交易首选和投资判断不会自动覆盖，必须由你确认。" />
          </Form>
        </section>

        <section className="trend-update-center-section is-launch">
          <div className="trend-update-center-heading"><div><Typography.Title level={4}>发起全面更新</Typography.Title><Typography.Text>上次完成：{formatTime(researchSettings.data?.last_researched_at)}</Typography.Text></div><Tag color="green">全面覆盖 · 增量输出</Tag></div>
          <Form form={fullUpdateForm} layout="vertical">
            <Form.Item name="clue" label="本次线索"><TextArea rows={3} placeholder="例如：重点检查Google OCS是否向外部供应链扩散" /></Form.Item>
            <Form.Item name="requirements" label="本次临时要求"><TextArea rows={2} placeholder="不会写入长期设定" /></Form.Item>
          </Form>
          <Button block size="large" type="primary" icon={<RobotOutlined />} disabled={Boolean(activeJob)} loading={action.isPending} onClick={() => action.mutateAsync(startFullUpdate)}>{activeJob ? "全面更新正在进行" : "开始全面更新"}</Button>
        </section>

        <section className="trend-update-center-section">
          <div className="trend-update-center-heading"><div><Typography.Title level={4}>任务记录</Typography.Title><Typography.Text>Token为本地按文本长度估算，用于比较每次更新成本。</Typography.Text></div><Button size="small" icon={<ReloadOutlined />} onClick={() => queryClient.invalidateQueries({ queryKey: ["industry-trend-jobs", selectedId] })}>刷新</Button></div>
          <List
            loading={generationJobs.isLoading}
            dataSource={generationJobs.data?.items ?? []}
            locale={{ emptyText: "还没有全面更新记录" }}
            renderItem={(item) => (
              <List.Item actions={item.status === "succeeded" && selected?.draft ? [<Button key="draft" size="small" onClick={() => setDraftOpen(true)}>查看草稿</Button>] : undefined}>
                <List.Item.Meta
                  title={<Space wrap><strong>{item.update_mode}</strong><Tag color={item.status === "succeeded" ? "green" : item.status === "failed" ? "red" : item.status === "running" ? "blue" : undefined}>{JOB_STATUS_LABELS[item.status] ?? item.status}</Tag><span>{item.phase}</span></Space>}
                  description={(
                    <div className="trend-job-description">
                      <Space wrap><span>{formatTime(item.created_at)}</span><span>变化 {item.change_count}</span><span>Token估算 {item.total_token_estimate.toLocaleString()}</span></Space>
                      {item.error_message ? <div className="trend-job-error">失败原因：{generationErrorText(item.error_message)}</div> : null}
                    </div>
                  )}
                />
              </List.Item>
            )}
          />
        </section>
      </Drawer>

      <Modal
        open={Boolean(nodeEditing)}
        rootClassName="industry-node-modal"
        centered
        title={(
          <div className="industry-node-modal-title">
            <div className="industry-node-modal-title-row">
              <strong>{nodeEditing === "new" ? "新增产业节点" : activeNode?.name || "产业节点详情"}</strong>
              {activeNode ? <Space wrap size={5}><Tag color="blue">{activeNodeTypeMeta?.title || activeNode.node_type}</Tag>{activeNode.maturity_status ? <Tag color="green">{activeNode.maturity_status}</Tag> : null}<Tag>重要性 {activeNode.investment_importance || "待判断"}</Tag></Space> : null}
            </div>
            <span>{nodeEditing === "new" ? "补充产业链中的关键环节" : nodeFormEditing ? "修改节点判断与展示信息" : "从商业逻辑、公司和证据判断这个环节是否值得跟踪"}</span>
          </div>
        )}
        width={960}
        onCancel={closeNode}
        footer={nodeEditing === "new" || nodeFormEditing ? [
          <Button key="cancel" onClick={cancelNodeEdit}>{nodeEditing === "new" ? "取消" : "返回详情"}</Button>,
          <Button key="save" type="primary" icon={<SaveOutlined />} loading={action.isPending} onClick={() => action.mutateAsync(saveNode)}>保存修改</Button>
        ] : [
          <Button key="close" onClick={closeNode}>关闭</Button>,
          <Button key="edit" type="primary" icon={<EditOutlined />} onClick={() => setNodeFormEditing(true)}>编辑节点</Button>
        ]}
      >
        {activeNode && !nodeFormEditing ? (
          <NodeDetailView industryName={selected?.name || ""} node={activeNode} companies={activeNodeCompanies} validations={activeNodeValidations} sources={activeNodeSources} onViewCompany={(company) => setCompanyViewingId(company.id)} />
        ) : (
        <Form form={nodeForm} layout="vertical" className="industry-node-form">
          <section className="industry-node-form-section is-basic">
            <div className="industry-node-section-heading">
              <span>基本信息</span>
              <small>它在产业链中的位置与成熟度</small>
            </div>
            <div className="industry-node-basic-grid">
              <Form.Item className="is-wide" name="name" label="节点名称" rules={[{ required: true }]}>
                <Input autoFocus />
              </Form.Item>
              <Form.Item name="node_type" label="节点类型" rules={[{ required: true }]}>
                <Select options={NODE_TYPES.map((value) => ({ value, label: nodeTypeMeta(selected?.name || "", value).title || value }))} />
              </Form.Item>
              <Form.Item name="maturity_status" label="当前成熟度">
                <Select allowClear options={NODE_MATURITY_STATUSES.map((value) => ({ value }))} />
              </Form.Item>
            </div>
          </section>

          <section className="industry-node-form-section is-reading">
            <div className="industry-node-section-heading">
              <span>快速看懂这个环节</span>
              <small>先看它是什么，再看价值如何兑现</small>
            </div>
            <div className="industry-node-reading-list">
              <div className="industry-node-reading-item">
                <b>1</b>
                <Form.Item name="plain_explanation" label="这是什么">
                  <TextArea rows={2} placeholder="尽量不用行业黑话，让第一次接触该行业的人也能看懂" />
                </Form.Item>
              </div>
              <div className="industry-node-reading-item is-value">
                <b>2</b>
                <Form.Item name="value_flow" label="钱为什么流到这里">
                  <TextArea rows={2} placeholder="需求如何变成这个环节的订单、收入或利润" />
                </Form.Item>
              </div>
              <div className="industry-node-reading-item is-check">
                <b>3</b>
                <Form.Item name="watch_signal" label="后面看什么验证">
                  <TextArea rows={2} placeholder="订单、出货、价格、良率、客户认证或毛利率" />
                </Form.Item>
              </div>
            </div>
          </section>

          <section className="industry-node-form-section">
            <div className="industry-node-section-heading">
              <span>产业判断</span>
              <small>用于判断空间、壁垒和竞争位置</small>
            </div>
            <div className="industry-node-judgement-grid">
              <Form.Item name="market_space" label="应用场景或市场空间">
                <TextArea rows={2} placeholder="需求来自哪里，空间有多大" />
              </Form.Item>
              <Form.Item name="tech_barrier" label="技术壁垒">
                <TextArea rows={2} placeholder="难点、认证、工艺或规模门槛" />
              </Form.Item>
              <Form.Item className="is-wide" name="competition" label="竞争格局">
                <TextArea rows={2} placeholder="主要参与者、份额变化与供给集中度" />
              </Form.Item>
            </div>
          </section>

          <section className="industry-node-form-section is-attributes">
            <div className="industry-node-section-heading">
              <span>投资属性</span>
              <small>用于产业地图展示与排序</small>
            </div>
            <div className="industry-node-attribute-grid">
              <Form.Item name="profit_elasticity" label="利润弹性">
                <Select allowClear placeholder="未判断" options={["强", "中", "弱"].map((value) => ({ value }))} />
              </Form.Item>
              <Form.Item name="localization" label="国产替代">
                <Select allowClear placeholder="未判断" options={["强", "中", "弱"].map((value) => ({ value }))} />
              </Form.Item>
              <Form.Item name="investment_importance" label="投资重要性">
                <Select allowClear placeholder="未判断" options={["强", "中", "弱"].map((value) => ({ value }))} />
              </Form.Item>
              <Form.Item name="sort_order" label="地图排序">
                <InputNumber className="industry-node-sort-input" />
              </Form.Item>
            </div>
          </section>
        </Form>
        )}
      </Modal>

      <CompanyDetailModal
        company={viewedCompany}
        detail={selected ?? null}
        calibrationGate={expectationSummary.data?.companies.find((item) => item.company_id === viewedCompany?.id)?.expectation_gap_gate || null}
        methodCalibration={expectationSummary.data?.expectation_gap_calibration || null}
        onClose={() => setCompanyViewingId(null)}
        onOpenStock={(company) => {
          setCompanyViewingId(null);
          if (company.full_code) navigate(`/stocks/${company.full_code}`);
        }}
        onEditCompany={(company) => {
          setCompanyViewingId(null);
          openCompany(company);
        }}
      />

      <Modal open={edgeOpen} title="管理节点连线" footer={null} onCancel={() => setEdgeOpen(false)} width={660}><Form form={edgeForm} layout="inline" onFinish={() => action.mutateAsync(saveEdge)}><Form.Item name="from_node_id" rules={[{ required: true }]}><Select style={{ width: 210 }} placeholder="起点" options={(selected?.nodes ?? []).map((node) => ({ label: node.name, value: node.id }))} /></Form.Item><ArrowRightOutlined /><Form.Item name="to_node_id" rules={[{ required: true }]}><Select style={{ width: 210 }} placeholder="终点" options={(selected?.nodes ?? []).map((node) => ({ label: node.name, value: node.id }))} /></Form.Item><Button htmlType="submit" type="primary">新增连线</Button></Form><List className="trend-edge-list" dataSource={selected?.edges ?? []} renderItem={(edge) => <List.Item actions={[<Button key="delete" type="text" danger icon={<DeleteOutlined />} onClick={() => selectedId && action.mutateAsync(() => industryTrendApi.deleteEdge(selectedId, edge.id)).then(() => invalidate())} />]}>{selected?.nodes.find((node) => node.id === edge.from_node_id)?.name} <ArrowRightOutlined /> {selected?.nodes.find((node) => node.id === edge.to_node_id)?.name}</List.Item>} /></Modal>

      <Modal open={Boolean(companyEditing)} title={companyEditing === "new" ? "新增产业公司" : "编辑产业公司"} okText="保存" onOk={() => action.mutateAsync(saveCompany)} onCancel={() => setCompanyEditing(null)} confirmLoading={action.isPending} width={800}>
        <Form form={companyForm} layout="vertical">
          <Space wrap>
            <Form.Item name="market" label="市场" rules={[{ required: true }]}><Select disabled={companyEditing !== "new"} style={{ width: 110 }} options={COMPANY_MARKETS.map((value) => ({ value }))} /></Form.Item>
            <Form.Item name="exchange" label="交易所"><Input disabled={companyEditing !== "new"} placeholder="NASDAQ / TPEx / KOSDAQ / TSE" /></Form.Item>
            <Form.Item name="code" label="公司代码" rules={[{ required: companyEditing === "new" }]}><Input disabled={companyEditing !== "new"} placeholder={companyMarket === "A股" ? "例如 300308" : "例如 COHR、3163、5801"} /></Form.Item>
            <Form.Item name="name" label="公司名称" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="is_global_leader" label="推荐逻辑1" valuePropName="checked"><Checkbox>全球绝对优势公司</Checkbox></Form.Item>
            <Form.Item name="is_domestic_alternative" label="推荐逻辑2" valuePropName="checked"><Checkbox disabled={companyMarket !== "A股"}>国内可替代公司</Checkbox></Form.Item>
            <Form.Item name="is_primary" label="交易表达" valuePropName="checked"><Checkbox disabled={companyMarket !== "A股"}>设为唯一A股交易首选</Checkbox></Form.Item>
          </Space>
          <Form.Item name="external_url" label="公司官网或投资者关系链接"><Input placeholder="海外公司建议填写官方页面" /></Form.Item>
          <Form.Item name="node_ids" label="所属产业节点"><Select mode="multiple" options={(selected?.nodes ?? []).map((node) => ({ label: node.name, value: node.id }))} /></Form.Item>
          <Space wrap><Form.Item name="position" label="产业位置"><Input /></Form.Item><Form.Item name="company_standing" label="公司地位"><Input /></Form.Item><Form.Item name="benefit_directness" label="受益直接性"><Select allowClear options={["强", "中", "弱"].map((value) => ({ value }))} /></Form.Item></Space>
          <Form.Item name="core_advantage" label="核心优势"><TextArea rows={2} /></Form.Item>
          <Form.Item name="profit_path" label="利润兑现路径"><TextArea rows={2} /></Form.Item>
          <Space wrap><Form.Item name="verification_status" label="验证状态"><Select options={VERIFICATION_STATUSES.map((value) => ({ value }))} /></Form.Item><Form.Item name="tracking_status" label="关注状态"><Select options={TRACKING_STATUSES.map((value) => ({ value }))} /></Form.Item><Form.Item name="pricing_status" label="市场定价"><Select options={PRICING_STATUSES.map((value) => ({ value }))} /></Form.Item><Form.Item name="sort_order" label="排序"><InputNumber /></Form.Item></Space>
          <Form.Item name="primary_reason" label="推荐或交易首选依据"><TextArea rows={2} placeholder="全球优势看份额、技术、客户和交付；国内替代写清替代对象、认证与量产进度" /></Form.Item>
          <Form.Item name="main_risk" label="主要风险"><TextArea rows={2} /></Form.Item>
          <section className="trend-company-expectation-form">
            <header><div><strong>市值与预期差</strong><span>先反推市场在相信什么，再写硬证据能支持什么；信息不全就选“无法判断”</span></div>{companyBeingEdited?.market_cap_snapshot?.total_market_cap ? <Button size="small" onClick={() => companyForm.setFieldsValue({ expectation_as_of: new Date().toISOString().slice(0, 10), expectation_anchor_market_cap_yi: companyBeingEdited.market_cap_snapshot!.total_market_cap / 100_000_000 })}>按当前市值重设锚</Button> : null}</header>
            <div className="trend-company-expectation-form-grid">
              <Form.Item name="expectation_gap_status" label="预期差状态" rules={[{ required: true }]}><Select options={EXPECTATION_GAP_STATUSES.map((value) => ({ value }))} /></Form.Item>
              <Form.Item name="expectation_as_of" label="判断日期"><Input type="date" /></Form.Item>
              <Form.Item name="expectation_anchor_market_cap_yi" label="判断时总市值（亿元）"><InputNumber min={0} precision={1} style={{ width: "100%" }} /></Form.Item>
            </div>
            <div className="trend-company-evidence-number-grid">
              <Form.Item name="expectation_evidence_growth_pct" label="硬证据业绩增速（%）" extra="填公司公告、财报或订单可验证的利润/业绩同比增速，不填机构预测"><InputNumber precision={1} style={{ width: "100%" }} placeholder="例如 65" /></Form.Item>
              <Form.Item name="expectation_evidence_acceleration_pct" label="相对前期加速度（百分点）" extra="当前增速减前一可比期增速；用于判断是否出现第一次加速"><InputNumber precision={1} style={{ width: "100%" }} placeholder="例如 25" /></Form.Item>
            </div>
            <div className="trend-company-expectation-text-grid">
              <Form.Item name="market_implied_expectation" label="当前市值隐含预期"><TextArea rows={3} placeholder="例如：当前市值需要2027年利润达到多少、份额升到多少、市场已经提前交易到哪一年" /></Form.Item>
              <Form.Item name="evidence_based_expectation" label="硬证据支持的预期"><TextArea rows={3} placeholder="只写公告、财报、订单、价格、客户认证、收入、毛利和现金流能支持的区间" /></Form.Item>
            </div>
            <Form.Item name="expectation_gap_reason" label="为什么存在这项预期差"><TextArea rows={2} placeholder="明确写两边差在订单、利润、份额、持续时间还是兑现年份" /></Form.Item>
            <div className="trend-company-expectation-text-grid">
              <Form.Item name="expectation_trigger" label="升级触发器"><TextArea rows={2} placeholder="什么时间、出现什么指标，正向预期差才能被确认" /></Form.Item>
              <Form.Item name="expectation_invalidation" label="证伪条件"><TextArea rows={2} placeholder="什么数据出现后必须下调或取消预期差" /></Form.Item>
            </div>
          </section>
        </Form>
      </Modal>

      <Modal open={Boolean(catalystEditing)} title={catalystEditing === "new" ? "新增关键催化事件" : "编辑关键催化事件"} okText="保存" onOk={() => action.mutateAsync(saveCatalyst)} onCancel={() => setCatalystEditing(null)} confirmLoading={action.isPending} width={720}>
        <Form form={catalystForm} layout="vertical">
          <Form.Item name="event_name" label="事件名称" rules={[{ required: true }]}><Input autoFocus placeholder="例如：云厂商上调资本开支指引" /></Form.Item>
          <Space wrap>
            <Form.Item name="expected_time" label="预计时间"><Input placeholder="例如 Q3、Q3-Q4、2026-09" /></Form.Item>
            <Form.Item name="event_type" label="事件类型"><Select style={{ width: 160 }} options={CATALYST_TYPES.map((value) => ({ value }))} /></Form.Item>
            <Form.Item name="importance" label="重要程度"><Select style={{ width: 100 }} options={["高", "中", "低"].map((value) => ({ value }))} /></Form.Item>
            <Form.Item name="status" label="当前状态"><Select style={{ width: 110 }} options={CATALYST_STATUSES.map((value) => ({ value }))} /></Form.Item>
          </Space>
          <Form.Item name="impact" label="预期影响"><TextArea rows={2} placeholder="它将确认什么，并可能推动产业进入哪个阶段" /></Form.Item>
          <Space wrap>
            <Form.Item name="impact_node_id" label="影响环节"><Select allowClear style={{ width: 190 }} options={(selected?.nodes ?? []).map((node) => ({ label: node.name, value: node.id }))} /></Form.Item>
            <Form.Item name="impact_company_id" label="影响公司"><Select allowClear style={{ width: 190 }} options={(selected?.companies ?? []).map((company) => ({ label: company.name, value: company.id }))} /></Form.Item>
            <Form.Item name="sort_order" label="排序"><InputNumber /></Form.Item>
          </Space>
          <Space wrap><Form.Item name="source_name" label="来源"><Input /></Form.Item><Form.Item name="source_url" label="来源链接"><Input style={{ width: 340 }} /></Form.Item></Space>
        </Form>
      </Modal>

      <Modal open={Boolean(validationEditing)} title={validationEditing === "new" ? "新增验证节点" : "编辑验证节点"} okText="保存" onOk={() => action.mutateAsync(saveValidation)} onCancel={() => setValidationEditing(null)} confirmLoading={action.isPending} width={680}><Form form={validationForm} layout="vertical"><Form.Item name="name" label="验证事项" rules={[{ required: true }]}><Input autoFocus /></Form.Item><Form.Item name="criteria" label="确认标准"><TextArea rows={2} /></Form.Item><Form.Item name="current_result" label="当前结果"><TextArea rows={2} /></Form.Item><Space wrap><Form.Item name="status" label="状态"><Select options={VERIFICATION_STATUSES.map((value) => ({ value }))} /></Form.Item><Form.Item name="node_id" label="产业节点"><Select allowClear style={{ width: 170 }} options={(selected?.nodes ?? []).map((node) => ({ label: node.name, value: node.id }))} /></Form.Item><Form.Item name="company_id" label="关联股票"><Select allowClear style={{ width: 170 }} options={(selected?.companies ?? []).map((company) => ({ label: company.name, value: company.id }))} /></Form.Item><Form.Item name="target_date" label="目标日期"><Input type="date" /></Form.Item><Form.Item name="sort_order" label="路线顺序"><InputNumber min={0} /></Form.Item></Space><Space wrap><Form.Item name="source_name" label="来源"><Input /></Form.Item><Form.Item name="source_url" label="来源链接"><Input style={{ width: 300 }} /></Form.Item></Space></Form></Modal>

      <Modal open={Boolean(updateEditing)} title={updateEditing === "new" ? "新增变化或因果信号" : "编辑变化或因果信号"} okText="保存" onOk={() => action.mutateAsync(saveUpdate)} onCancel={() => setUpdateEditing(null)} confirmLoading={action.isPending} width={780}>
        <Form form={updateForm} layout="vertical">
          <Space wrap align="start">
            <Form.Item name="update_date" label="当时日期" rules={[{ required: true }]}><Input type="date" /></Form.Item>
            <Form.Item name="causal_stage" label="因果阶段"><Select allowClear style={{ width: 140 }} options={CAUSAL_STAGES.map((value) => ({ value }))} /></Form.Item>
            <Form.Item name="evidence_type" label="证据类型"><Select allowClear style={{ width: 130 }} options={CAUSAL_EVIDENCE_TYPES.map((value) => ({ value }))} /></Form.Item>
            <Form.Item name="signal_status" label="当前状态"><Select allowClear style={{ width: 120 }} options={CAUSAL_SIGNAL_STATUSES.map((value) => ({ value }))} /></Form.Item>
            <Form.Item name="sell_pressure" label="潜在卖压"><Select allowClear style={{ width: 100 }} options={SELL_PRESSURES.map((value) => ({ value }))} /></Form.Item>
          </Space>
          <Form.Item name="content" label="当时出现了什么" rules={[{ required: true }]}><TextArea rows={3} placeholder="只写当时能够观察到的变化，不用后来结果倒推" /></Form.Item>
          <Form.Item name="impact" label="为什么会影响未来买盘"><TextArea rows={2} placeholder="说明预期差如何产生，不能只写利好" /></Form.Item>
          <Form.Item name="buyer_group" label="下一批可能买入的资金"><Input placeholder="例如产业资金、机构、趋势资金、普通投资者；无法识别可留空" /></Form.Item>
          <Form.Item name="market_response" label="当时盘面如何表达"><TextArea rows={2} placeholder="价格、成交额、板块共振、回撤承接；行情只是确认，不等于原因" /></Form.Item>
          <Form.Item name="counter_evidence" label="当时已有的反向证据"><TextArea rows={2} placeholder="需求、订单、估值、筹码或技术路线中不支持判断的部分" /></Form.Item>
          <Form.Item name="next_verification" label="下一步验证或证伪"><TextArea rows={2} /></Form.Item>
          <Space wrap><Form.Item name="source_name" label="来源"><Input /></Form.Item><Form.Item name="source_url" label="来源链接"><Input style={{ width: 380 }} /></Form.Item></Space>
          <Space><Form.Item name="affects_phase" valuePropName="checked"><Checkbox>影响产业阶段</Checkbox></Form.Item><Form.Item name="affects_decision" valuePropName="checked"><Checkbox>影响投资判断</Checkbox></Form.Item></Space>
          <Form.Item noStyle shouldUpdate={(prev, next) => prev.affects_phase !== next.affects_phase}>{({ getFieldValue }) => getFieldValue("affects_phase") ? <Form.Item name="phase_suggestion" label="阶段建议"><Select options={PHASES.map((value) => ({ value }))} /></Form.Item> : null}</Form.Item>
        </Form>
      </Modal>

      <Drawer title="Codex研究草稿" width={720} open={draftOpen} onClose={() => setDraftOpen(false)} extra={selected?.draft ? <Space><Popconfirm title="丢弃该草稿？" onConfirm={() => industryTrendApi.deleteDraft(selected.id).then(() => { setDraftOpen(false); invalidate(); })}><Button danger>丢弃</Button></Popconfirm><Button type="primary" onClick={() => action.mutateAsync(() => industryTrendApi.applyDraft(selected.id)).then(() => { setDraftOpen(false); invalidate(); message.success("草稿已应用"); })}>确认应用</Button></Space> : null}>{selected?.draft ? <><Alert type="warning" showIcon message="草稿不会自动覆盖正式研究" description={`类型：${selected.draft.draft_type} · 来源：${selected.draft.source}`} /><DraftReview draft={selected.draft} /></> : <Empty description="暂无草稿" />}</Drawer>

      <Drawer title="产业研究历史" width={720} open={historyOpen} onClose={() => setHistoryOpen(false)}><List loading={versions.isLoading} dataSource={versions.data?.items ?? []} renderItem={(item) => <List.Item actions={[<Popconfirm key="restore" title={`恢复第${item.revision}版？恢复后会生成新版本。`} onConfirm={() => selectedId && industryTrendApi.restore(selectedId, item.id).then(() => { setHistoryOpen(false); invalidate(); message.success("历史版本已恢复"); })}><Button type="link" icon={<ReloadOutlined />}>恢复</Button></Popconfirm>]}><List.Item.Meta title={`第 ${item.revision} 版`} description={formatTime(item.created_at)} /></List.Item>} /></Drawer>
    </main>
  );
}
