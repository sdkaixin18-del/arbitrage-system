import { BellOutlined, DeleteOutlined, EditOutlined, HistoryOutlined, PauseCircleOutlined, PlayCircleOutlined, ReloadOutlined, SettingOutlined, SoundOutlined, SwapOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Input, InputNumber, Modal, Popconfirm, Select, Slider, Space, Switch, Table, Tag, Tooltip, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  type CryptoCoinTransferStatus,
  type CryptoExchange,
  type CryptoFundingCapEvent,
  type CryptoFundingCapExchangeSnapshot,
  type CryptoFundingFormationTarget,
  type CryptoFundingFormationWatchResponse,
  type CryptoFundingPredictionReviewItem,
  type CryptoFsSignal,
  type CryptoFsSignalCheck,
  type CryptoMarketType,
  type CryptoSymbolMapping,
  type CryptoSymbolMappingScanResponse
} from "../api";
import { cryptoApi as api } from "../api/crypto";
import AppChart from "../components/AppChart";
import TransferStatusWatch from "../components/TransferStatusWatch";

const signalLimit = 20;
const minVisibleOpenSpreadRate = 0.003;
const defaultQualifiedDailyFundingRate = -0.01;

const exchangeLabel: Record<string, string> = {
  bn: "Binance",
  by: "Bybit",
  gt: "Gate",
  okx: "OKX",
  bg: "Bitget",
  as: "Aster"
};

const defaultExchangeOptions = Object.entries(exchangeLabel)
  .filter(([value]) => ["bn", "by", "gt", "okx", "bg"].includes(value))
  .map(([value, label]) => ({ value: value as CryptoExchange, label }));

const fundingFormationLineColor: Record<string, string> = {
  bn: "#d99a00",
  by: "#f05a47",
  gt: "#20a77b",
  okx: "#24292f",
  bg: "#1677ff"
};

const marketTypeOptions: { value: CryptoMarketType; label: string }[] = [
  { value: "futures", label: "合约 (Futures)" },
  { value: "spot", label: "现货 (Spot)" }
];

const priceRatioOptions = [
  { value: 1e-9, label: "1/B (10^-9)" },
  { value: 1e-6, label: "1/M (10^-6)" },
  { value: 0.001, label: "1/K (0.001)" },
  { value: 0.0001, label: "1:10000 (0.0001)" },
  { value: 1, label: "1" },
  { value: 1000, label: "K (1000)" },
  { value: 10000, label: "10000:1 (10000)" },
  { value: 1e6, label: "M (10^6)" },
  { value: 1e9, label: "B (10^9)" }
];

interface MappingDraft {
  exchange: CryptoExchange | null;
  marketType: CryptoMarketType;
  inputSymbol: string;
  mappedSymbol: string;
  priceRatio: number;
  note: string;
}

type FsGroupedRow = CryptoFsSignal & {
  variants: CryptoFsSignal[];
};

interface FundingFormationMonitor {
  id: string;
  exchange: CryptoExchange;
  symbol: string;
  targetRate?: number;
}

interface AstroBlockedPairDraft {
  marketKey: string;
  symbol: string;
}

const fsAlarmSettingsStorageKey = "fs-alarm-settings-v1";
const fsAlarmEventsStorageKey = "fs-alarm-events-v1";

interface FsAlarmSettings {
  enabled: boolean;
  soundEnabled: boolean;
  borrowableOpenEnabled: boolean;
  changeEnabled: boolean;
  customEnabled: boolean;
  maxDailyFundingPercent: number;
  minOpenSpreadPercent: number;
  minNetFundingPercent: number;
  cooldownSeconds: number;
  volumePercent: number;
}

type FsAlarmEventType = "borrowable_open" | "change" | "custom" | "test";

interface FsAlarmEvent {
  id: string;
  type: FsAlarmEventType;
  symbol: string;
  title: string;
  detail: string;
  createdAt: string;
}

interface FsAlarmSnapshot {
  symbol: string;
  borrowPlatforms: string[];
  borrowableOpen: boolean;
  customMatched: boolean;
  signature: string;
  detail: string;
}

const defaultFsAlarmSettings: FsAlarmSettings = {
  enabled: true,
  soundEnabled: true,
  borrowableOpenEnabled: true,
  changeEnabled: true,
  customEnabled: false,
  maxDailyFundingPercent: -1.5,
  minOpenSpreadPercent: 0,
  minNetFundingPercent: 0,
  cooldownSeconds: 60,
  volumePercent: 100
};

function loadFsAlarmSettings(): FsAlarmSettings {
  try {
    const stored = window.localStorage.getItem(fsAlarmSettingsStorageKey);
    const parsed = stored ? JSON.parse(stored) : null;
    return parsed && typeof parsed === "object"
      ? { ...defaultFsAlarmSettings, ...parsed }
      : defaultFsAlarmSettings;
  } catch {
    return defaultFsAlarmSettings;
  }
}

function loadFsAlarmEvents(): FsAlarmEvent[] {
  try {
    const stored = window.localStorage.getItem(fsAlarmEventsStorageKey);
    const parsed = stored ? JSON.parse(stored) : null;
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (item): item is FsAlarmEvent =>
          item &&
          typeof item.id === "string" &&
          typeof item.type === "string" &&
          typeof item.symbol === "string" &&
          typeof item.title === "string" &&
          typeof item.detail === "string" &&
          typeof item.createdAt === "string"
      )
      .slice(0, 30);
  } catch {
    return [];
  }
}

let fsAlarmAudioContext: AudioContext | null = null;

function playFsAlarmSound(volumePercent = 100) {
  const AudioContextConstructor =
    window.AudioContext ??
    (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextConstructor) return;
  fsAlarmAudioContext ??= new AudioContextConstructor();
  const context = fsAlarmAudioContext;
  const play = () => {
    const startAt = context.currentTime + 0.02;
    const master = context.createGain();
    const compressor = context.createDynamicsCompressor();
    master.gain.value = Math.max(0.2, Math.min(1.5, volumePercent / 100));
    compressor.threshold.value = -18;
    compressor.knee.value = 12;
    compressor.ratio.value = 8;
    compressor.attack.value = 0.003;
    compressor.release.value = 0.2;
    master.connect(compressor);
    compressor.connect(context.destination);
    [0, 0.27, 0.54, 0.81].forEach((offset, index) => {
      const noteStart = startAt + offset;
      [
        { frequency: index % 2 === 0 ? 820 : 1080, type: "square" as OscillatorType, peak: 0.5 },
        { frequency: index % 2 === 0 ? 1230 : 1620, type: "sine" as OscillatorType, peak: 0.28 }
      ].forEach((tone) => {
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.type = tone.type;
        oscillator.frequency.value = tone.frequency;
        gain.gain.setValueAtTime(0.0001, noteStart);
        gain.gain.exponentialRampToValueAtTime(tone.peak, noteStart + 0.015);
        gain.gain.setValueAtTime(tone.peak, noteStart + 0.12);
        gain.gain.exponentialRampToValueAtTime(0.0001, noteStart + 0.21);
        oscillator.connect(gain);
        gain.connect(master);
        oscillator.start(noteStart);
        oscillator.stop(noteStart + 0.23);
      });
    });
  };
  if (context.state === "suspended") {
    void context.resume().then(play).catch(() => undefined);
  } else {
    play();
  }
}

function fundingFormationMonitorId(exchange: CryptoExchange, symbol: string) {
  return `${exchange}:${symbol.toUpperCase()}`;
}

function fundingFormationMonitorsFromWatch(data?: CryptoFundingFormationWatchResponse): FundingFormationMonitor[] {
  return (data?.items ?? []).map((item) => ({
    id: fundingFormationMonitorId(item.exchange, item.symbol),
    exchange: item.exchange,
    symbol: item.symbol,
    targetRate: item.targetRate ?? undefined
  }));
}

function emptyMappingDraft(): MappingDraft {
  return {
    exchange: null,
    marketType: "spot",
    inputSymbol: "",
    mappedSymbol: "",
    priceRatio: 1,
    note: ""
  };
}

const beijingFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false
});

function timeText(value?: string | null) {
  if (!value) return "-";
  const normalized = /([zZ]|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return beijingFormatter.format(date).replace(/\//g, "-");
}

function rateText(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(3)}%`;
}

function astroSpreadText(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return (value * 100).toFixed(2);
}

function amountText(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 1000000) return `${(value / 1000000).toFixed(2)}M`;
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(2)}K`;
  return value.toFixed(value >= 10 ? 2 : 4);
}

function astroFundingUrl(symbol: string) {
  return `https://funding-v2.astro-btc.xyz/?coin=${encodeURIComponent(symbol.toUpperCase())}`;
}

function timeMs(value?: string | null) {
  if (!value) return Number.POSITIVE_INFINITY;
  const normalized = /([zZ]|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? Number.POSITIVE_INFINITY : date.getTime();
}

function signalPriority(signal: CryptoFsSignal) {
  if (signal.actionable) return 2;
  if (signal.watchOnly) return 1;
  return 0;
}

function signalChecks(signal: CryptoFsSignal): CryptoFsSignalCheck[] {
  return Object.values(signal.checks ?? {}).filter((check): check is CryptoFsSignalCheck => Boolean(check));
}

function signalHasBorrowInventory(signal: CryptoFsSignal) {
  if (typeof signal.executableBorrow === "boolean") return signal.executableBorrow;
  return signalChecks(signal).some((check) => check.canBorrow === true);
}

function signalDailyFundingRate(signal: CryptoFsSignal) {
  if (typeof signal.dailyFundingRate === "number" && Number.isFinite(signal.dailyFundingRate)) {
    return signal.dailyFundingRate;
  }
  if (
    typeof signal.currentFundingRate === "number" &&
    Number.isFinite(signal.currentFundingRate) &&
    typeof signal.periodHours === "number" &&
    Number.isFinite(signal.periodHours) &&
    signal.periodHours > 0
  ) {
    return signal.currentFundingRate * 24 / signal.periodHours;
  }
  return null;
}

function buildFsAlarmSnapshot(row: FsGroupedRow, settings: FsAlarmSettings): FsAlarmSnapshot {
  const borrowPlatforms = Array.from(
    new Set(
      row.variants.flatMap((variant) =>
        signalChecks(variant)
          .filter((check) => check.canBorrow === true)
          .map((check) => exchangeLabel[check.exchange] ?? check.exchange)
      )
    )
  ).sort();
  const borrowableOpen = row.variants.some(
    (variant) => signalHasBorrowInventory(variant) && variant.actionable
  );
  const customMatched = row.variants.some((variant) => {
    if (!signalHasBorrowInventory(variant)) return false;
    const dailyFunding = signalDailyFundingRate(variant);
    const openSpread = variant.openSpreadRate ?? variant.spreadRate;
    const netFunding = variant.netFundingRate;
    return (
      dailyFunding != null &&
      dailyFunding * 100 <= settings.maxDailyFundingPercent &&
      typeof openSpread === "number" &&
      Number.isFinite(openSpread) &&
      openSpread * 100 >= settings.minOpenSpreadPercent &&
      typeof netFunding === "number" &&
      Number.isFinite(netFunding) &&
      netFunding * 100 >= settings.minNetFundingPercent
    );
  });
  const stateParts = row.variants
    .map((variant) => {
      const available = signalHasBorrowInventory(variant);
      const checks = signalChecks(variant)
        .map((check) => `${check.exchange}:${check.canBorrow === true ? "1" : "0"}`)
        .sort()
        .join(",");
      return [
        variant.futuresExchange,
        available ? "B" : "-",
        variant.actionable ? "open" : variant.watchOnly ? "watch" : "idle",
        variant.limitOrderCandidate ? "limit" : "-",
        checks
      ].join(":");
    })
    .sort();
  const best = row.variants[0];
  const dailyFunding = signalDailyFundingRate(best);
  const openSpread = best.openSpreadRate ?? best.spreadRate;
  return {
    symbol: row.symbol,
    borrowPlatforms,
    borrowableOpen,
    customMatched,
    signature: stateParts.join("|"),
    detail: [
      borrowPlatforms.length ? `可借：${borrowPlatforms.join("、")}` : "当前无可借平台",
      `日化 ${rateText(dailyFunding)}`,
      `开仓差价 ${rateText(openSpread)}`,
      `净收益/期 ${rateText(best.netFundingRate)}`
    ].join(" · ")
  };
}

function compareSignals(left: CryptoFsSignal, right: CryptoFsSignal) {
  const borrowDiff = Number(signalHasBorrowInventory(right)) - Number(signalHasBorrowInventory(left));
  if (borrowDiff !== 0) return borrowDiff;

  const largeSpreadDiff = Number(right.limitOrderCandidate === true) - Number(left.limitOrderCandidate === true);
  if (largeSpreadDiff !== 0) return largeSpreadDiff;

  const priorityDiff = signalPriority(right) - signalPriority(left);
  if (priorityDiff !== 0) return priorityDiff;

  const leftNet = left.netFundingRate;
  const rightNet = right.netFundingRate;
  const leftHasNet = typeof leftNet === "number" && Number.isFinite(leftNet);
  const rightHasNet = typeof rightNet === "number" && Number.isFinite(rightNet);
  if (leftHasNet && rightHasNet && leftNet !== rightNet) return rightNet - leftNet;
  if (leftHasNet !== rightHasNet) return leftHasNet ? -1 : 1;

  const leftRate = left.currentFundingRate;
  const rightRate = right.currentFundingRate;
  const leftHasRate = typeof leftRate === "number" && Number.isFinite(leftRate);
  const rightHasRate = typeof rightRate === "number" && Number.isFinite(rightRate);
  if (leftHasRate && rightHasRate && leftRate !== rightRate) return leftRate - rightRate;
  if (leftHasRate !== rightHasRate) return leftHasRate ? -1 : 1;

  return timeMs(left.fundingTime) - timeMs(right.fundingTime);
}

function signalStableKey(signal: CryptoFsSignal) {
  return signal.signalKey || `${signal.symbol}-${signal.futuresExchange}-${signal.fundingTime ?? ""}`;
}

function borrowPlatformChecks(signal: CryptoFsSignal) {
  const platformOrder = ["bg", "bn"];
  const hardUnavailableMarkers = [
    "当前不支持借",
    "未开启抵押",
    "25112",
    "参数不存在",
    "不存在",
    "未返回"
  ];
  return signalChecks(signal)
    .filter((check) => {
      if (check.canBorrow === true) return true;
      if (check.status === "not_supported") return false;
      const message = check.message ?? "";
      return !hardUnavailableMarkers.some((marker) => message.includes(marker));
    })
    .sort((left, right) => platformOrder.indexOf(left.exchange) - platformOrder.indexOf(right.exchange));
}

function borrowPlatformTooltipText(check: CryptoFsSignalCheck, symbol: string) {
  const hasBorrowInventory = check.canBorrow === true;
  if (!hasBorrowInventory) return "无 B";

  const details = ["可借"];
  if (
    typeof check.borrowableAmount === "number" &&
    Number.isFinite(check.borrowableAmount)
  ) {
    details.push(`额度 ${amountText(check.borrowableAmount)} ${symbol}`);
  }
  if (typeof check.borrowPeriodRate === "number" && Number.isFinite(check.borrowPeriodRate)) {
    details.push(`成本 ${rateText(check.borrowPeriodRate)}`);
  }
  if (typeof check.netFundingRate === "number" && Number.isFinite(check.netFundingRate)) {
    details.push(`净收益 ${rateText(check.netFundingRate)}`);
  }
  return details.join(" · ");
}

function transferFlagText(value?: boolean | null, openText = "开", closedText = "停") {
  if (value === true) return openText;
  if (value === false) return closedText;
  return "未知";
}

function transferPlatformLabel(exchange: CryptoExchange) {
  if (exchange === "bn") return "BN";
  if (exchange === "bg") return "BG";
  return exchangeLabel[exchange] ?? exchange.toUpperCase();
}

function transferTooltip(exchange: CryptoExchange, status?: CryptoCoinTransferStatus | null) {
  const platform = transferPlatformLabel(exchange);
  if (!status) return `${platform} 尚未检查充提状态`;
  const chainText = (status.chains ?? [])
    .slice(0, 8)
    .map(
      (chain) =>
        `${chain.chain}: 充${transferFlagText(chain.depositEnabled)} / 提${transferFlagText(chain.withdrawEnabled)}`
    )
    .join("；");
  return [`${platform}：${status.message}`, chainText].filter(Boolean).join("；");
}

function transferSummary(exchange: CryptoExchange, status?: CryptoCoinTransferStatus | null) {
  const platform = transferPlatformLabel(exchange);
  if (!status || status.status === "pending" || status.status === "error") {
    return { text: `${platform} 未确认`, color: "default" as const };
  }
  if (status.status === "not_supported") {
    return { text: `${platform} 未上线`, color: "default" as const };
  }
  if (status.depositEnabled === false && status.withdrawEnabled === false) {
    return { text: `${platform} 充提停`, color: "red" as const };
  }
  if (status.depositEnabled === false) {
    return { text: `${platform} 充币停`, color: "red" as const };
  }
  if (status.withdrawEnabled === false) {
    return { text: `${platform} 提币停`, color: "red" as const };
  }
  const closedChains = (status.chains ?? []).filter((chain) => chain.depositEnabled === false || chain.withdrawEnabled === false);
  if (closedChains.length) {
    return { text: `${platform} 部分链停`, color: "orange" as const };
  }
  if (status.depositEnabled === true && status.withdrawEnabled === true) {
    return { text: `${platform} 正常`, color: "green" as const };
  }
  return { text: `${platform} 未确认`, color: "default" as const };
}

function renderTransferIssues(row: CryptoFsSignal) {
  const exchanges: CryptoExchange[] = ["bn", "bg"];
  const issues = exchanges.flatMap((exchange) => {
    const status = row.checks?.[exchange]?.transferStatus;
    const summary = transferSummary(exchange, status);
    if (
      summary.color !== "red" &&
      summary.color !== "orange" &&
      status?.status !== "not_supported"
    ) {
      return [];
    }
    return [{ exchange, status, summary }];
  });
  if (!issues.length) return null;
  return (
    <Space size={4} wrap>
      {issues.map(({ exchange, status, summary }) => (
        <Tooltip key={exchange} title={transferTooltip(exchange, status)}>
          <Tag color={summary.color}>{summary.text}</Tag>
        </Tooltip>
      ))}
    </Space>
  );
}

function fundingCapStatusColor(snapshot: CryptoFundingCapExchangeSnapshot) {
  if (snapshot.status === "ok") return "green";
  if (snapshot.status === "error") return "red";
  if (snapshot.status === "unknown") return "gold";
  return "default";
}

function fundingPeriodText(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "周期未知";
  return `${value.toFixed(Number.isInteger(value) ? 0 : 1)}h`;
}

function compactRatePercent(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "费率未知";
  const percent = Number((value * 100).toFixed(4));
  return `${Object.is(percent, -0) ? 0 : percent}%`;
}

function directionalFundingLimit(
  currentFundingRate?: number | null,
  maxFundingRate?: number | null,
) {
  if (typeof maxFundingRate !== "number" || !Number.isFinite(maxFundingRate)) return null;
  const magnitude = Math.abs(maxFundingRate);
  return typeof currentFundingRate === "number"
    && Number.isFinite(currentFundingRate)
    && currentFundingRate < 0
    ? -magnitude
    : magnitude;
}

function fundingRuleSnapshotText(snapshot: CryptoFundingCapExchangeSnapshot) {
  if (snapshot.status === "not_supported") return "无合约";
  return `${compactRatePercent(snapshot.maxFundingRate)} -${fundingPeriodText(snapshot.fundingIntervalHours)}`;
}

function fundingRuleNumberChanged(previous?: number | null, current?: number | null) {
  return (
    typeof previous === "number"
    && Number.isFinite(previous)
    && typeof current === "number"
    && Number.isFinite(current)
    && Math.abs(previous - current) > 1e-10
  );
}

function fundingRuleEventText(event: CryptoFundingCapEvent) {
  const changes: string[] = [];
  if (event.changeKinds.includes("cap")) {
    if (fundingRuleNumberChanged(event.previousMaxFundingRate, event.currentMaxFundingRate)) {
      changes.push(
        `最大费率 ${compactRatePercent(event.previousMaxFundingRate)} → ${compactRatePercent(event.currentMaxFundingRate)}`
      );
    }
  }
  if (event.changeKinds.includes("interval")) {
    changes.push(
      `周期 ${fundingPeriodText(event.previousFundingIntervalHours)} → ${fundingPeriodText(event.currentFundingIntervalHours)}`
    );
  }
  return changes.join("；") || "资金费规则已变化";
}

function fundingCountdown(minutes?: number | null) {
  if (typeof minutes !== "number" || !Number.isFinite(minutes)) return "-";
  const totalMinutes = Math.max(0, Math.floor(minutes));
  const hours = Math.floor(totalMinutes / 60);
  const rest = totalMinutes % 60;
  return hours > 0 ? `${hours}小时 ${rest}分` : `${rest}分钟`;
}

function fundingFormationTargetShortText(target: CryptoFundingFormationTarget) {
  if (target.status === "insufficient_history" || target.status === "impossible" || target.status === "invalid") {
    return target.message ?? "数据不足";
  }
  if (target.status === "band") {
    return `${rateText(target.premiumBoundaryLow)} ～ ${rateText(target.premiumBoundaryHigh)}`;
  }
  if (target.status === "settled") {
    return target.reached ? "已达到" : "未达到";
  }
  const relation = target.relation === "lte" ? "≤" : "≥";
  if (target.status === "estimated") {
    return `估算 ${relation} ${rateText(
      target.estimatedRequiredPremiumRate ?? target.requiredPremiumRate,
    )}`;
  }
  return `${relation} ${rateText(target.requiredPremiumRate)}`;
}

function fundingFormationTargetDetail(
  target: CryptoFundingFormationTarget | undefined,
  accuracyMessage: string,
) {
  if (
    !target
    || typeof target.requiredPremiumRangeLow !== "number"
    || typeof target.requiredPremiumRangeHigh !== "number"
  ) {
    return accuracyMessage;
  }
  return `${accuracyMessage} 估算区间 ${rateText(target.requiredPremiumRangeLow)} ～ ${rateText(target.requiredPremiumRangeHigh)}。`;
}

function fundingFormationCoverageText(coverage: number) {
  const percent = coverage * 100;
  return `${percent.toFixed(percent >= 99.95 ? 0 : 1)}%`;
}

export default function FsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [mappingOpen, setMappingOpen] = useState(false);
  const [mappingDraft, setMappingDraft] = useState<MappingDraft>(() => emptyMappingDraft());
  const [editingMappingId, setEditingMappingId] = useState<number | null>(null);
  const [savingMapping, setSavingMapping] = useState(false);
  const [deletingMappingId, setDeletingMappingId] = useState<number | null>(null);
  const [mappingScanResult, setMappingScanResult] = useState<CryptoSymbolMappingScanResponse | null>(null);
  const [fundingCapWatchInput, setFundingCapWatchInput] = useState("");
  const [fundingCapWatchExchanges, setFundingCapWatchExchanges] = useState<CryptoExchange[]>([]);
  const [fundingFormationExchanges, setFundingFormationExchanges] = useState<CryptoExchange[]>(["bn"]);
  const [fundingFormationSymbolInput, setFundingFormationSymbolInput] = useState("BTC");
  const [fundingFormationTargetInput, setFundingFormationTargetInput] = useState("");
  const [fundingFormationChartId, setFundingFormationChartId] = useState<string | null>(null);
  const [fundingCapExchangeEditor, setFundingCapExchangeEditor] = useState<{
    symbol: string;
    exchanges: CryptoExchange[];
  } | null>(null);
  const [alarmSettingsOpen, setAlarmSettingsOpen] = useState(false);
  const [alarmHistoryOpen, setAlarmHistoryOpen] = useState(false);
  const [astroSubscriptionsOpen, setAstroSubscriptionsOpen] = useState(false);
  const [astroSubscriptionDraft, setAstroSubscriptionDraft] = useState<string[]>([]);
  const [astroMinVolumeDraft, setAstroMinVolumeDraft] = useState(10000);
  const [astroBlockedPairDraft, setAstroBlockedPairDraft] = useState<AstroBlockedPairDraft[]>([]);
  const [astroBlockMarketDraft, setAstroBlockMarketDraft] = useState("");
  const [astroBlockSymbolDraft, setAstroBlockSymbolDraft] = useState("");
  const [fundingPredictionReviewOpen, setFundingPredictionReviewOpen] = useState(false);
  const [fundingPredictionReviewDays, setFundingPredictionReviewDays] = useState(30);
  const [fundingPredictionReviewCheckpoint, setFundingPredictionReviewCheckpoint] = useState(15);
  const [fundingPredictionReviewModel, setFundingPredictionReviewModel] = useState("");
  const [alarmSettings, setAlarmSettings] = useState<FsAlarmSettings>(() => loadFsAlarmSettings());
  const [alarmEvents, setAlarmEvents] = useState<FsAlarmEvent[]>(() => loadFsAlarmEvents());
  const previousAlarmSnapshotsRef = useRef<Map<string, FsAlarmSnapshot> | null>(null);
  const processedAlarmUpdateRef = useRef<string | null>(null);
  const alarmCooldownRef = useRef(new Map<string, number>());
  const fundingWatch = useQuery({
    queryKey: ["fs-funding-formation-watch"],
    queryFn: () => api.fsFundingFormationWatch(),
    refetchInterval: 30000,
    retry: false
  });
  const fundingFormationMonitors = useMemo(
    () => fundingFormationMonitorsFromWatch(fundingWatch.data),
    [fundingWatch.data]
  );
  const saveFundingWatch = useMutation({
    mutationFn: async (change: { additions?: FundingFormationMonitor[]; removeId?: string }) => {
      // Merge explicit edits into a fresh server list, never a browser snapshot.
      const current = fundingFormationMonitorsFromWatch(await api.fsFundingFormationWatch());
      const merged = new Map(current.map((item) => [item.id, item]));
      for (const item of change.additions ?? []) merged.set(item.id, item);
      if (change.removeId) merged.delete(change.removeId);
      if (merged.size > 25) throw new Error("最多监控 25 个组合，请先移除部分组合");
      return api.syncFsFundingFormationWatch(Array.from(merged.values()));
    },
    onMutate: () => queryClient.cancelQueries({ queryKey: ["fs-funding-formation-watch"] }),
    onSuccess: (data, change) => {
      queryClient.setQueryData(["fs-funding-formation-watch"], data);
      void queryClient.invalidateQueries({ queryKey: ["fs-funding-cloud-status"] });
      if (change.additions?.length) setFundingFormationChartId(change.additions[0].id);
      message.success(change.removeId ? "已停止该组合监控" : "资金费监控已保存");
    },
    onError: (error: Error) => {
      message.error(`监控保存失败：${error.message}`);
      void queryClient.invalidateQueries({ queryKey: ["fs-funding-formation-watch"] });
    }
  });
  const signals = useQuery({
    queryKey: ["fs-signals", signalLimit],
    queryFn: () => api.fsSignals(signalLimit),
    refetchInterval: 10000
  });
  const astroAutoCardStatus = useQuery({
    queryKey: ["astro-auto-card-status"],
    queryFn: () => api.astroAutoCardStatus(),
    refetchInterval: 10000,
    retry: false
  });
  const scheduler = useQuery({
    queryKey: ["fs-scheduler"],
    queryFn: () => api.fsScheduler(),
    refetchInterval: 60000
  });
  const fsSettings = useQuery({
    queryKey: ["fs-settings"],
    queryFn: () => api.fsSettings(),
    enabled: mappingOpen
  });
  const fundingCapWatch = useQuery({
    queryKey: ["fs-funding-cap-watch"],
    queryFn: () => api.fsFundingCapWatch(),
    refetchInterval: 60000,
    retry: false
  });
  const fundingFormationBatch = useQuery({
    queryKey: ["fs-funding-formation-batch", fundingFormationMonitors],
    queryFn: () => api.fsFundingFormationBatch(fundingFormationMonitors),
    enabled: fundingFormationMonitors.length > 0,
    refetchInterval: 30000,
    retry: false
  });
  const fundingCloudStatus = useQuery({
    queryKey: ["fs-funding-cloud-status"],
    queryFn: () => api.fsFundingCloudStatus(),
    refetchInterval: 60000,
    retry: false
  });
  const fundingPredictionReview = useQuery({
    queryKey: [
      "fs-funding-prediction-review",
      fundingPredictionReviewDays,
      fundingPredictionReviewCheckpoint,
      fundingPredictionReviewModel
    ],
    queryFn: () => api.fsFundingPredictionReview(
      fundingPredictionReviewDays,
      fundingPredictionReviewCheckpoint,
      fundingPredictionReviewModel
    ),
    enabled: fundingPredictionReviewOpen,
    refetchInterval: 30000,
    retry: false
  });
  const fundingFormationResultsById = useMemo(
    () => new Map(
      (fundingFormationBatch.data?.items ?? []).map((item) => [item.id, item])
    ),
    [fundingFormationBatch.data?.items]
  );
  const fundingFormationPeriodMismatchIds = useMemo(() => {
    const byExchange = new Map<CryptoExchange, { id: string; period: number }[]>();
    fundingFormationMonitors.forEach((monitor) => {
      const period = fundingFormationResultsById.get(monitor.id)?.data?.fundingIntervalHours;
      if (typeof period !== "number" || !Number.isFinite(period)) return;
      byExchange.set(monitor.exchange, [
        ...(byExchange.get(monitor.exchange) ?? []),
        { id: monitor.id, period }
      ]);
    });
    const mismatches = new Set<string>();
    for (const rows of byExchange.values()) {
      if (rows.length < 2) continue;
      const counts = new Map<string, number>();
      rows.forEach(({ period }) => {
        const key = period.toFixed(8);
        counts.set(key, (counts.get(key) ?? 0) + 1);
      });
      if (counts.size < 2) continue;
      const highestCount = Math.max(...counts.values());
      const dominantPeriods = Array.from(counts.entries())
        .filter(([, count]) => count === highestCount)
        .map(([period]) => period);
      if (dominantPeriods.length === 1) {
        rows
          .filter(({ period }) => period.toFixed(8) !== dominantPeriods[0])
          .forEach(({ id }) => mismatches.add(id));
      } else {
        rows.forEach(({ id }) => mismatches.add(id));
      }
    }
    return mismatches;
  }, [fundingFormationMonitors, fundingFormationResultsById]);
  const fundingFormationChartMonitor = fundingFormationChartId
    ? fundingFormationMonitors.find((monitor) => monitor.id === fundingFormationChartId) ?? null
    : null;
  const fundingFormationChartResults = useMemo(() => {
    const symbol = fundingFormationChartMonitor?.symbol;
    if (!symbol) return [];
    return fundingFormationMonitors
      .filter((monitor) => monitor.symbol === symbol)
      .map((monitor) => fundingFormationResultsById.get(monitor.id)?.data ?? null)
      .filter((result): result is NonNullable<typeof result> => Boolean(result));
  }, [fundingFormationChartMonitor?.symbol, fundingFormationMonitors, fundingFormationResultsById]);
  const fundingFormationPremiumChartOption = useMemo(() => {
    const series = fundingFormationChartResults
      .filter((result) => (result.premiumAverageHistory?.length ?? 0) > 0)
      .map((result) => ({
        name: `${result.exchangeName} ${fundingPeriodText(result.fundingIntervalHours)}`,
        type: "line",
        showSymbol: false,
        smooth: false,
        data: (result.premiumAverageHistory ?? []).map((point) => [
          point.timestamp,
          point.averagePremiumRate * 100
        ]),
        lineStyle: {
          width: 2,
          color: fundingFormationLineColor[result.exchange] ?? "#1677ff"
        },
        itemStyle: { color: fundingFormationLineColor[result.exchange] ?? "#1677ff" }
      }));
    if (!series.length) return null;
    const cycleStartTimes = fundingFormationChartResults
      .map((result) => Date.parse(result.cycleStartTime))
      .filter(Number.isFinite);
    const historyEndTimes = fundingFormationChartResults
      .map((result) => Date.parse(result.historyWindowEndTime))
      .filter(Number.isFinite);
    return {
      animation: false,
      grid: { left: 58, right: 22, top: 18, bottom: 38 },
      tooltip: {
        trigger: "axis",
        valueFormatter: (value: string | number) => `${Number(value).toFixed(4)}%`
      },
      xAxis: {
        type: "time",
        min: cycleStartTimes.length ? Math.min(...cycleStartTimes) : undefined,
        max: historyEndTimes.length ? Math.max(...historyEndTimes) : undefined,
        boundaryGap: false,
        axisLabel: { color: "#7f8b85", fontSize: 10 },
        axisLine: { lineStyle: { color: "#dce6e1" } }
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: {
          color: "#7f8b85",
          fontSize: 10,
          formatter: (value: number) => `${value.toFixed(3)}%`
        },
        splitLine: { lineStyle: { color: "#edf2ef" } }
      },
      series
    };
  }, [fundingFormationChartResults]);
  useEffect(() => {
    setFundingFormationChartId((current) => {
      if (current && fundingFormationMonitors.some((monitor) => monitor.id === current)) {
        return current;
      }
      return fundingFormationMonitors[0]?.id ?? null;
    });
  }, [fundingFormationMonitors]);
  useEffect(() => {
    window.localStorage.setItem(fsAlarmSettingsStorageKey, JSON.stringify(alarmSettings));
  }, [alarmSettings]);
  useEffect(() => {
    window.localStorage.setItem(fsAlarmEventsStorageKey, JSON.stringify(alarmEvents.slice(0, 30)));
  }, [alarmEvents]);
  const addFundingCapWatch = useMutation({
    mutationFn: ({ symbol, exchanges }: { symbol: string; exchanges: CryptoExchange[] }) =>
      api.addFsFundingCapWatch(symbol, exchanges),
    onSuccess: (data) => {
      queryClient.setQueryData(["fs-funding-cap-watch"], data);
      setFundingCapWatchInput("");
      setFundingCapWatchExchanges([]);
      message.success("已添加资金费规则监控");
    },
    onError: (error) => message.error(`添加失败：${String(error)}`)
  });
  const updateFundingCapWatch = useMutation({
    mutationFn: ({ symbol, exchanges }: { symbol: string; exchanges: CryptoExchange[] }) =>
      api.updateFsFundingCapWatch(symbol, exchanges),
    onSuccess: (data) => {
      queryClient.setQueryData(["fs-funding-cap-watch"], data);
      setFundingCapExchangeEditor(null);
      message.success("监控交易所已更新");
    },
    onError: (error) => message.error(`更新失败：${String(error)}`)
  });
  const deleteFundingCapWatch = useMutation({
    mutationFn: (symbol: string) => api.deleteFsFundingCapWatch(symbol),
    onSuccess: (data) => {
      queryClient.setQueryData(["fs-funding-cap-watch"], data);
      message.success("已停止监控");
    },
    onError: (error) => message.error(`删除失败：${String(error)}`)
  });
  const refreshFundingCapWatch = useMutation({
    mutationFn: () => api.refreshFsFundingCapWatch(),
    onSuccess: (data) => {
      queryClient.setQueryData(["fs-funding-cap-watch"], data);
      message.success(data.scan.message);
    },
    onError: (error) => message.error(`检查失败：${String(error)}`)
  });
  const saveAstroSubscriptions = useMutation({
    mutationFn: (payload: {
      markets: string[];
      minVolumeUsdt: number;
      blockedPairs: AstroBlockedPairDraft[];
      blockedCoins: string[];
    }) => api.updateAstroSpreadSubscriptions(payload),
    onSuccess: (data) => {
      queryClient.setQueryData(["astro-auto-card-status"], data);
      queryClient.setQueryData(["fs-signals", signalLimit], (previous: typeof signals.data) =>
        previous ? { ...previous, astroAutoCard: data } : previous
      );
      setAstroSubscriptionsOpen(false);
      message.success(`Astro 扫描规则已保存，当前 ${data.spreadScanner?.subscriptions.length ?? 0} 个行情源`);
    },
    onError: (error) => message.error(`Astro 扫描规则保存失败：${String(error)}`)
  });
  const startScan = useMutation({
    mutationFn: () => api.startFsScheduler(),
    onSuccess: (data) => {
      queryClient.setQueryData(["fs-scheduler"], data);
      queryClient.invalidateQueries({ queryKey: ["fs-signals", signalLimit] });
      message.success("FS 自动扫描已开始");
    },
    onError: (error) => message.error(`开始失败：${String(error)}`)
  });
  const pauseScan = useMutation({
    mutationFn: () => api.pauseFsScheduler(),
    onSuccess: (data) => {
      queryClient.setQueryData(["fs-scheduler"], data);
      queryClient.setQueryData(["fs-signals", signalLimit], (previous: typeof signals.data) =>
        previous
          ? {
              ...previous,
              scanning: false,
              message: "自动扫描已暂停。"
            }
          : previous
      );
      message.success("FS 自动扫描已暂停");
    },
    onError: (error) => message.error(`暂停失败：${String(error)}`)
  });
  const scanMappings = useMutation({
    mutationFn: () => api.scanFsSymbolMappings(),
    onSuccess: async (data) => {
      setMappingScanResult(data);
      await fsSettings.refetch();
      queryClient.invalidateQueries({ queryKey: ["fs-signals", signalLimit] });
      message.success(
        `映射扫描完成：别名 ${data.aliasCandidateCount}，新增 ${data.createdCount}，更新 ${data.updatedCount}`
      );
    },
    onError: (error) => message.error(`映射扫描失败：${String(error)}`)
  });

  const fsRows = useMemo<FsGroupedRow[]>(() => {
    const qualifiedDailyFundingRate =
      signals.data?.watchDailyThreshold ??
      signals.data?.watchThreshold ??
      defaultQualifiedDailyFundingRate;
    const uniqueSignals = new Map<string, CryptoFsSignal>();
    for (const item of [...(signals.data?.items ?? []), ...(signals.data?.watchItems ?? [])]) {
      if (!item.negativePotential && !item.actionable && !item.watchOnly) continue;
      const hasBorrowInventory = signalHasBorrowInventory(item);
      const dailyFundingRate =
        typeof item.dailyFundingRate === "number" && Number.isFinite(item.dailyFundingRate)
          ? item.dailyFundingRate
          : (
              typeof item.currentFundingRate === "number" &&
              Number.isFinite(item.currentFundingRate) &&
              typeof item.periodHours === "number" &&
              Number.isFinite(item.periodHours) &&
              item.periodHours > 0
            )
            ? item.currentFundingRate * 24 / item.periodHours
            : null;
      if (
        hasBorrowInventory &&
        (
          dailyFundingRate == null ||
          dailyFundingRate > qualifiedDailyFundingRate
        )
      ) continue;
      if (
        !hasBorrowInventory &&
        (
          typeof item.openSpreadRate !== "number" ||
          !Number.isFinite(item.openSpreadRate) ||
          item.openSpreadRate < minVisibleOpenSpreadRate
        )
      ) continue;
      const key = signalStableKey(item);
      const existing = uniqueSignals.get(key);
      uniqueSignals.set(key, existing && compareSignals(existing, item) <= 0 ? existing : item);
    }

    const bySymbol = new Map<string, CryptoFsSignal[]>();
    for (const item of uniqueSignals.values()) {
      const key = item.symbol.toUpperCase();
      bySymbol.set(key, [...(bySymbol.get(key) ?? []), item]);
    }

    return Array.from(bySymbol.entries())
      .map(([symbol, variants]) => {
        const sorted = [...variants].sort(compareSignals);
        return { ...sorted[0], symbol, variants: sorted };
      })
      .sort(compareSignals);
  }, [
    signals.data?.items,
    signals.data?.watchDailyThreshold,
    signals.data?.watchItems,
    signals.data?.watchThreshold
  ]);
  const schedulerEnabled = scheduler.data?.enabled ?? true;
  const scanActive = Boolean((schedulerEnabled && (signals.data?.scanning || scheduler.data?.scanning)) || startScan.isPending);
  const schedulerStatusText = scanActive
    ? "扫描中"
    : schedulerEnabled
      ? `每 ${scheduler.data?.intervalSeconds ?? 60}s 检查`
      : "已暂停";
  const astroCardStatus = astroAutoCardStatus.data ?? signals.data?.astroAutoCard;
  const astroScannerStatus = astroCardStatus?.spreadScanner;
  const astroMarketsByExchange = useMemo(() => {
    const grouped = new Map<string, NonNullable<typeof astroScannerStatus>["availableMarkets"]>();
    for (const market of astroScannerStatus?.availableMarkets ?? []) {
      grouped.set(market.exchangeName, [...(grouped.get(market.exchangeName) ?? []), market]);
    }
    return Array.from(grouped.entries());
  }, [astroScannerStatus?.availableMarkets]);

  const openAstroSubscriptions = () => {
    setAstroSubscriptionDraft([...(astroScannerStatus?.subscriptions ?? [])]);
    setAstroMinVolumeDraft(astroScannerStatus?.minVolumeUsdt ?? 10000);
    setAstroBlockedPairDraft([...(astroScannerStatus?.blockedPairs ?? [])]);
    setAstroBlockMarketDraft(astroScannerStatus?.subscriptions[0] ?? "");
    setAstroBlockSymbolDraft("");
    setAstroSubscriptionsOpen(true);
  };

  const toggleAstroSubscription = (key: string, checked: boolean) => {
    setAstroSubscriptionDraft((current) =>
      checked
        ? Array.from(new Set([...current, key]))
        : current.filter((item) => item !== key)
    );
  };

  const addAstroBlockedPair = () => {
    const symbol = astroBlockSymbolDraft.trim().toUpperCase().replace(/USDT$/, "");
    if (!astroBlockMarketDraft || !symbol || !/^[A-Z0-9]+$/.test(symbol)) {
      message.warning("请选择行情源并输入正确币种");
      return;
    }
    const normalized = `${symbol}USDT`;
    if (astroBlockedPairDraft.some((item) => item.marketKey === astroBlockMarketDraft && item.symbol === normalized)) {
      message.info("该过滤规则已存在");
      return;
    }
    setAstroBlockedPairDraft((current) => [
      ...current,
      { marketKey: astroBlockMarketDraft, symbol: normalized }
    ]);
    setAstroBlockSymbolDraft("");
  };

  const astroMarketLabel = (marketKey: string) => {
    const market = astroScannerStatus?.availableMarkets.find((item) => item.key === marketKey);
    if (!market) return marketKey;
    return `${market.exchangeName} ${market.marketType === "spot" ? "现货" : "合约"}`;
  };

  const scrollToAlarmCenter = () => {
    setAlarmHistoryOpen(true);
    window.setTimeout(() => {
      document.getElementById("fs-alarm-center")?.scrollIntoView({
        behavior: "smooth",
        block: "start"
      });
    }, 80);
  };

  const requestDesktopAlarmPermission = () => {
    if (!("Notification" in window) || window.Notification.permission !== "default") return;
    void window.Notification.requestPermission().catch(() => undefined);
  };

  const presentAlarmEvents = (events: FsAlarmEvent[], forceSound = false) => {
    if (!events.length) return;
    setAlarmEvents((current) => [...events, ...current].slice(0, 30));
    if (forceSound || alarmSettings.soundEnabled) playFsAlarmSound(alarmSettings.volumePercent);
    if ("Notification" in window && window.Notification.permission === "granted") {
      const notification = new window.Notification(events[0].title, {
        body: events.length === 1 ? events[0].detail : `${events[0].detail}；另有 ${events.length - 1} 条变化。`,
        tag: "fs-trading-monitor-alarm"
      });
      notification.onclick = () => {
        window.focus();
        notification.close();
        scrollToAlarmCenter();
      };
    }
    Modal.confirm({
      title: (
        <Space wrap className="fs-page-head-actions">
          <BellOutlined />
          <span>交易监控报警</span>
        </Space>
      ),
      content: (
        <div className="fs-alarm-dialog-list">
          {events.slice(0, 5).map((event) => (
            <div key={event.id}>
              <strong>{event.title}</strong>
              <span>{event.detail}</span>
            </div>
          ))}
          {events.length > 5 ? <Typography.Text type="secondary">另有 {events.length - 5} 条变化</Typography.Text> : null}
        </div>
      ),
      okText: "查看报警",
      cancelText: "关闭",
      centered: true,
      onOk: scrollToAlarmCenter
    });
  };

  const testAlarm = () => {
    requestDesktopAlarmPermission();
    const event: FsAlarmEvent = {
      id: `test-${Date.now()}`,
      type: "test",
      symbol: "测试",
      title: "声音报警测试",
      detail: "报警声音和弹窗正常；点击“查看报警”可直达报警记录。",
      createdAt: new Date().toISOString()
    };
    presentAlarmEvents([event], true);
  };

  useEffect(() => {
    const updateMarker = signals.data?.updatedAt ?? null;
    if (!updateMarker || processedAlarmUpdateRef.current === updateMarker) return;
    processedAlarmUpdateRef.current = updateMarker;

    const nextSnapshots = new Map(
      fsRows.map((row) => [row.symbol, buildFsAlarmSnapshot(row, alarmSettings)])
    );
    const previousSnapshots = previousAlarmSnapshotsRef.current;
    previousAlarmSnapshotsRef.current = nextSnapshots;
    if (!previousSnapshots || !alarmSettings.enabled) return;

    const now = Date.now();
    const cooldownMs = Math.max(10, alarmSettings.cooldownSeconds) * 1000;
    const events: FsAlarmEvent[] = [];
    const canNotify = (key: string) => {
      const lastAt = alarmCooldownRef.current.get(key) ?? 0;
      if (now - lastAt < cooldownMs) return false;
      alarmCooldownRef.current.set(key, now);
      return true;
    };

    for (const snapshot of nextSnapshots.values()) {
      const previous = previousSnapshots.get(snapshot.symbol);
      if (
        alarmSettings.borrowableOpenEnabled &&
        snapshot.borrowableOpen &&
        !previous?.borrowableOpen &&
        canNotify(`borrowable_open:${snapshot.symbol}`)
      ) {
        events.push({
          id: `borrowable-open-${snapshot.symbol}-${now}`,
          type: "borrowable_open",
          symbol: snapshot.symbol,
          title: `${snapshot.symbol} 有可借 B，可开单`,
          detail: snapshot.detail,
          createdAt: new Date(now).toISOString()
        });
        continue;
      }
      if (
        alarmSettings.customEnabled &&
        snapshot.customMatched &&
        !previous?.customMatched &&
        canNotify(`custom:${snapshot.symbol}`)
      ) {
        events.push({
          id: `custom-${snapshot.symbol}-${now}`,
          type: "custom",
          symbol: snapshot.symbol,
          title: `${snapshot.symbol} 满足自定义报警规则`,
          detail: snapshot.detail,
          createdAt: new Date(now).toISOString()
        });
        continue;
      }
      if (
        alarmSettings.changeEnabled &&
        (!previous || snapshot.signature !== previous.signature) &&
        canNotify(`change:${snapshot.symbol}`)
      ) {
        events.push({
          id: `change-${snapshot.symbol}-${now}`,
          type: "change",
          symbol: snapshot.symbol,
          title: previous ? `${snapshot.symbol} 状态发生变化` : `${snapshot.symbol} 新进入机会列表`,
          detail: snapshot.detail,
          createdAt: new Date(now).toISOString()
        });
      }
    }

    if (alarmSettings.changeEnabled) {
      for (const previous of previousSnapshots.values()) {
        if (!nextSnapshots.has(previous.symbol) && canNotify(`change:${previous.symbol}`)) {
          events.push({
            id: `removed-${previous.symbol}-${now}`,
            type: "change",
            symbol: previous.symbol,
            title: `${previous.symbol} 已移出机会列表`,
            detail: "当前扫描结果已不再满足交易监控展示条件。",
            createdAt: new Date(now).toISOString()
          });
        }
      }
    }
    presentAlarmEvents(events);
  }, [alarmSettings, fsRows, signals.data?.updatedAt]);
  const exchangeOptions = useMemo(
    () =>
      fsSettings.data?.availableExchanges
        ? fsSettings.data.availableExchanges.map((exchange) => ({ value: exchange.code, label: exchange.name }))
        : defaultExchangeOptions,
    [fsSettings.data?.availableExchanges]
  );
  const fundingCapExchangeOptions = useMemo(
    () =>
      fundingCapWatch.data?.exchanges?.length
        ? fundingCapWatch.data.exchanges.map((exchange) => ({ value: exchange.code, label: exchange.name }))
        : [
            ...defaultExchangeOptions,
            { value: "as" as CryptoExchange, label: "Aster" }
          ],
    [fundingCapWatch.data?.exchanges]
  );

  const submitFundingCapWatch = (value: string) => {
    const symbol = value.trim().toUpperCase();
    if (!symbol) {
      message.warning("请输入币种");
      return;
    }
    if (!fundingCapWatchExchanges.length) {
      message.warning("请选择要监控的交易所");
      return;
    }
    addFundingCapWatch.mutate({ symbol, exchanges: fundingCapWatchExchanges });
  };

  const submitFundingFormation = () => {
    const symbols = Array.from(
      new Set(
        fundingFormationSymbolInput
          .toUpperCase()
          .split(/[\s,，]+/)
          .map((symbol) => symbol.trim())
          .filter(Boolean)
      )
    );
    if (!symbols.length) {
      message.warning("请输入至少一个币种");
      return;
    }
    if (!fundingFormationExchanges.length) {
      message.warning("请选择至少一个交易所");
      return;
    }
    const targetText = fundingFormationTargetInput.trim();
    const targetPercent = targetText === "" ? undefined : Number(targetText);
    if (targetPercent !== undefined && (!Number.isFinite(targetPercent) || Math.abs(targetPercent) > 100)) {
      message.warning("自定义目标请输入 -100 到 +100 之间的百分比");
      return;
    }
    const additions = symbols.flatMap((symbol) =>
      fundingFormationExchanges.map((exchange) => ({
        id: fundingFormationMonitorId(exchange, symbol),
        exchange,
        symbol,
        targetRate: targetPercent === undefined ? undefined : targetPercent / 100
      }))
    );
    if (saveFundingWatch.isPending || !fundingWatch.data || fundingWatch.isError) return;
    saveFundingWatch.mutate({ additions });
  };

  const removeFundingFormationMonitor = (id: string) => {
    if (saveFundingWatch.isPending || fundingWatch.isError) return;
    saveFundingWatch.mutate({ removeId: id });
  };

  const resetMappingDraft = () => {
    setEditingMappingId(null);
    setMappingDraft(emptyMappingDraft());
  };

  const startEditMapping = (mapping: CryptoSymbolMapping) => {
    setEditingMappingId(mapping.id);
    setMappingDraft({
      exchange: mapping.exchange,
      marketType: mapping.marketType,
      inputSymbol: mapping.inputSymbol,
      mappedSymbol: mapping.mappedSymbol,
      priceRatio: mapping.priceRatio || 1,
      note: mapping.note ?? ""
    });
  };

  const saveMapping = async () => {
    const inputSymbol = mappingDraft.inputSymbol.trim().toUpperCase();
    const mappedSymbol = mappingDraft.mappedSymbol.trim().toUpperCase();
    if (!mappingDraft.exchange || !inputSymbol || !mappedSymbol) {
      message.warning("请填写交易所、原始名和映射名");
      return;
    }
    setSavingMapping(true);
    try {
      const payload = {
        inputSymbol,
        exchange: mappingDraft.exchange,
        marketType: mappingDraft.marketType,
        mappedSymbol,
        priceRatio: mappingDraft.priceRatio,
        note: mappingDraft.note.trim() || null
      };
      if (editingMappingId) {
        await api.updateFsSymbolMapping(editingMappingId, payload);
        message.success("映射已更新");
      } else {
        await api.createFsSymbolMapping(payload);
        message.success("映射已新增");
      }
      resetMappingDraft();
      await fsSettings.refetch();
      queryClient.invalidateQueries({ queryKey: ["fs-signals", signalLimit] });
    } catch (error) {
      message.error(`保存失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setSavingMapping(false);
    }
  };

  const deleteMapping = async (mapping: CryptoSymbolMapping) => {
    setDeletingMappingId(mapping.id);
    try {
      await api.deleteFsSymbolMapping(mapping.id);
      message.success("映射已删除");
      if (editingMappingId === mapping.id) resetMappingDraft();
      await fsSettings.refetch();
      queryClient.invalidateQueries({ queryKey: ["fs-signals", signalLimit] });
    } catch (error) {
      message.error(`删除失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setDeletingMappingId(null);
    }
  };

  const columns: ColumnsType<FsGroupedRow> = [
    {
      title: "币种",
      dataIndex: "symbol",
      width: 150,
      render: (value, row) => (
        <Space size={5}>
          <Typography.Link
            className="fs-symbol-link"
            strong
            href={astroFundingUrl(String(value))}
            target="_blank"
            rel="noreferrer"
          >
            {value}
          </Typography.Link>
          <Typography.Text type="secondary">
            - {exchangeLabel[row.futuresExchange] ?? row.futuresExchange}
          </Typography.Text>
        </Space>
      )
    },
    {
      title: "资金费 - 周期",
      dataIndex: "currentFundingRate",
      width: 135,
      sorter: (a, b) => (a.currentFundingRate ?? 0) - (b.currentFundingRate ?? 0),
      render: (value, row) => (
        <Space size={5}>
          <Tag color={typeof value === "number" && value < 0 ? "green" : "default"}>{rateText(value)}</Tag>
          <Typography.Text type="secondary">
            - {row.periodHours ? `${row.periodHours}h` : "-"}
          </Typography.Text>
        </Space>
      )
    },
    {
      title: "可借平台",
      width: 230,
      render: (_, row) => {
        const checks = borrowPlatformChecks(row);
        if (!checks.length) {
          return <Tag>{signalChecks(row).length ? "暂无可借平台" : "未检查"}</Tag>;
        }
        return (
          <Space size={4} wrap>
            {checks.map((check) => {
              const hasBorrowInventory = check.canBorrow === true;
              return (
                <Tooltip
                  key={check.exchange}
                  title={borrowPlatformTooltipText(check, row.symbol)}
                >
                  <Tag
                    className={`fs-borrow-platform-tag ${hasBorrowInventory ? "is-available" : "is-unavailable"}`}
                    color={hasBorrowInventory ? "green" : "default"}
                  >
                    {exchangeLabel[check.exchange] ?? check.exchange}
                  </Tag>
                </Tooltip>
              );
            })}
          </Space>
        );
      }
    },
    {
      title: "净收益/期",
      dataIndex: "netFundingRate",
      width: 116,
      sorter: (a, b) => (a.netFundingRate ?? -Infinity) - (b.netFundingRate ?? -Infinity),
      render: (value) => <Typography.Text strong>{rateText(value)}</Typography.Text>
    },
    {
      title: <Tooltip title="开仓 / 平仓；采用 Astro 对称差价公式">差价%</Tooltip>,
      width: 132,
      sorter: (a, b) => (a.openSpreadRate ?? -Infinity) - (b.openSpreadRate ?? -Infinity),
      render: (_, row) => {
        const openSpread = row.openSpreadRate ?? row.spreadRate;
        return (
          <Tooltip title="开仓：卖出现货买一 / 买入合约卖一；平仓：买回现货卖一 / 卖出合约买一">
            <Tag color={row.largeSpread ? "magenta" : "default"}>
              {astroSpreadText(openSpread)} / {astroSpreadText(row.closeSpreadRate)}
            </Tag>
          </Tooltip>
        );
      }
    },
    {
      title: "充提",
      width: 112,
      render: (_, row) => renderTransferIssues(row)
    }
  ];

  const variantColumns: ColumnsType<CryptoFsSignal> = [
    {
      title: "交易所",
      dataIndex: "futuresExchange",
      width: 96,
      render: (value) => exchangeLabel[value] ?? value
    },
    {
      title: "资金费 - 周期",
      dataIndex: "currentFundingRate",
      width: 140,
      render: (value, row) => (
        <Space size={5}>
          <Tag color={typeof value === "number" && value < 0 ? "green" : "default"}>{rateText(value)}</Tag>
          <Typography.Text type="secondary">
            - {row.periodHours ? `${row.periodHours}h` : "-"}
          </Typography.Text>
        </Space>
      )
    },
    {
      title: "净收益/期",
      dataIndex: "netFundingRate",
      width: 116,
      render: (value) => <Typography.Text strong>{rateText(value)}</Typography.Text>
    },
    {
      title: <Tooltip title="开仓 / 平仓；采用 Astro 对称差价公式">差价%</Tooltip>,
      width: 132,
      render: (_, row) => {
        const openSpread = row.openSpreadRate ?? row.spreadRate;
        return (
          <Tooltip title="开仓：卖出现货买一 / 买入合约卖一；平仓：买回现货卖一 / 卖出合约买一">
            <Tag color={row.largeSpread ? "magenta" : "default"}>
              {astroSpreadText(openSpread)} / {astroSpreadText(row.closeSpreadRate)}
            </Tag>
          </Tooltip>
        );
      }
    },
    {
      title: "充提",
      width: 112,
      render: (_, row) => renderTransferIssues(row)
    },
    {
      title: "BG 可借",
      width: 210,
      render: (_, row) => {
        const bgCheck = row.checks?.bg;
        if (!bgCheck) return <Typography.Text type="secondary">未检查</Typography.Text>;
        return (
          <Space size={4} direction="vertical">
            <Tag color={bgCheck.canBorrow ? "green" : "default"}>
              {bgCheck.canBorrow ? "可借" : "暂无 B"}
            </Tag>
            <Typography.Text type="secondary">{bgCheck.message ?? "-"}</Typography.Text>
          </Space>
        );
      }
    }
  ];

  const mappingColumns: ColumnsType<CryptoSymbolMapping> = [
    {
      title: "原始名",
      dataIndex: "inputSymbol",
      width: 88,
      render: (value) => <Typography.Text strong>{value}</Typography.Text>
    },
    {
      title: "交易所",
      dataIndex: "exchange",
      width: 96,
      render: (value: CryptoExchange) => exchangeLabel[value] ?? value
    },
    {
      title: "类型",
      dataIndex: "marketType",
      width: 86,
      render: (value: CryptoMarketType) => (value === "spot" ? "现货" : "合约")
    },
    {
      title: "映射名",
      dataIndex: "mappedSymbol",
      width: 110,
      render: (value) => <Typography.Text className="crypto-symbol-link">{value}</Typography.Text>
    },
    {
      title: "价格汇率",
      dataIndex: "priceRatio",
      width: 112,
      render: (value) => priceRatioOptions.find((option) => option.value === value)?.label ?? value
    },
    {
      title: "来源/备注",
      dataIndex: "note",
      width: 190,
      ellipsis: true,
      render: (value) => <Typography.Text type="secondary">{value || "-"}</Typography.Text>
    },
    {
      title: "操作",
      width: 88,
      align: "center",
      render: (_, mapping) => (
        <Space size={4}>
          <Tooltip title="编辑">
            <Button size="small" shape="circle" icon={<EditOutlined />} onClick={() => startEditMapping(mapping)} />
          </Tooltip>
          <Popconfirm title="删除这条映射？" okText="删除" cancelText="取消" onConfirm={() => deleteMapping(mapping)}>
            <Button size="small" shape="circle" danger icon={<DeleteOutlined />} loading={deletingMappingId === mapping.id} />
          </Popconfirm>
        </Space>
      )
    }
  ];

  const signalRankingBoard = (
    <section className="fs-ranked-section" id="borrow-monitor">
      <div className="fs-simple-section-title">
        <Space size={8}>
          <span className="fs-section-order">1</span>
          <Typography.Text strong>借币监控</Typography.Text>
          <Tag color={(signals.data?.borrowableCount ?? 0) > 0 ? "blue" : "default"}>
            可借 {signals.data?.borrowableCount ?? 0}
          </Tag>
          <Tag color={(signals.data?.actionableCount ?? 0) > 0 ? "green" : "default"}>
            可开 {signals.data?.actionableCount ?? 0}
          </Tag>
        </Space>
        <Typography.Text type="secondary">
          更新 {timeText(signals.data?.updatedAt)}
        </Typography.Text>
      </div>
      {signals.isError ? <Alert type="error" showIcon message="FS读取失败" description={String(signals.error)} /> : null}
      <div className="crypto-board crypto-fs-board">
        <Table
          className="fs-signals-table"
          size="small"
          rowKey={(row) => row.symbol}
          loading={signals.isLoading}
          columns={columns}
          dataSource={fsRows}
          pagination={false}
          tableLayout="fixed"
          scroll={{ x: 900 }}
          onRow={(row) => ({ id: `fs-signal-${row.symbol}` })}
          expandable={{
            rowExpandable: (row) => row.variants.length > 1,
            expandedRowRender: (row) => (
              <Table
                size="small"
                rowKey={(variant) => signalStableKey(variant)}
                pagination={false}
                dataSource={row.variants}
                columns={variantColumns}
                tableLayout="fixed"
                scroll={{ x: 820 }}
              />
            )
          }}
          locale={{ emptyText: "暂无达到资金费、结算窗口与风控条件的借币机会" }}
        />
      </div>
    </section>
  );

  return (
    <div className="page crypto-page fs-page">
      <div className="page-head">
        <div>
          <div className="fs-title-row">
            <Typography.Title level={1}>交易监控</Typography.Title>
            <div className={`fs-scan-state ${scanActive ? "scanning" : schedulerEnabled ? "running" : "paused"}`}>
              <span className="fs-scan-state-dot" />
              <span>{schedulerStatusText}</span>
            </div>
            <Button
              size="small"
              icon={<SettingOutlined />}
              onClick={() => navigate("/astro/status")}
              loading={astroAutoCardStatus.isLoading}
            >
              打开 Astro 建卡
            </Button>
          </div>
        </div>
        <Space>
          <Button icon={<SwapOutlined />} onClick={() => navigate("/astro/rules#symbol-mappings")}>
            币种映射
          </Button>
          <Button
            className={`fs-control-button ${!schedulerEnabled ? "is-emphasis" : ""}`}
            icon={<PlayCircleOutlined />}
            onClick={() => startScan.mutate()}
            loading={startScan.isPending}
            disabled={schedulerEnabled || startScan.isPending}
          >
            开始扫描
          </Button>
          <Button
            className={`fs-control-button ${schedulerEnabled ? "is-emphasis" : "is-paused-active"}`}
            icon={<PauseCircleOutlined />}
            onClick={() => pauseScan.mutate()}
            loading={pauseScan.isPending}
            disabled={!schedulerEnabled || pauseScan.isPending}
          >
            暂停扫描
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => signals.refetch()} loading={signals.isFetching}>
            刷新
          </Button>
        </Space>
      </div>

      <section
        id="fs-alarm-center"
        className={`fs-alarm-center ${alarmSettings.enabled ? "is-enabled" : "is-disabled"}`}
      >
        <div className="fs-alarm-toolbar">
          <div className="fs-alarm-identity">
            <BellOutlined />
            <Typography.Text strong>声音报警</Typography.Text>
            <Tag color={alarmSettings.enabled ? "green" : "default"}>
              {alarmSettings.enabled ? "开" : "关"}
            </Tag>
          </div>
          <div className="fs-alarm-compact-rules">
            <span className={alarmSettings.borrowableOpenEnabled ? "is-on" : ""}>有 B 可开单</span>
            <span className={alarmSettings.changeEnabled ? "is-on" : ""}>状态变化</span>
            {alarmSettings.customEnabled ? <span className="is-on">自定义</span> : null}
          </div>
          <Typography.Text className="fs-alarm-latest" type="secondary" ellipsis>
            {alarmEvents[0]
              ? `${alarmEvents[0].title} · ${timeText(alarmEvents[0].createdAt)}`
              : "暂无报警"}
          </Typography.Text>
          <Space size={6} className="fs-alarm-actions">
            <Button size="small" icon={<SoundOutlined />} onClick={testAlarm}>
              测试
            </Button>
            <Button size="small" icon={<SettingOutlined />} onClick={() => setAlarmSettingsOpen(true)}>
              规则
            </Button>
            <Button
              size="small"
              icon={<HistoryOutlined />}
              onClick={() => setAlarmHistoryOpen((open) => !open)}
            >
              记录{alarmEvents.length ? ` ${alarmEvents.length}` : ""}
            </Button>
          </Space>
        </div>
        {alarmHistoryOpen ? (
          <div className="fs-alarm-history">
            <div className="fs-alarm-history-head">
              <Typography.Text type="secondary">报警记录</Typography.Text>
              {alarmEvents.length ? (
                <Button type="text" size="small" onClick={() => setAlarmEvents([])}>
                  清空
                </Button>
              ) : null}
            </div>
            <div className="fs-alarm-events">
              {alarmEvents.length ? (
                alarmEvents.slice(0, 8).map((event) => (
                  <button
                    type="button"
                    className={`fs-alarm-event is-${event.type}`}
                    key={event.id}
                    onClick={() => {
                      if (event.symbol === "测试") return;
                      document.getElementById(`fs-signal-${event.symbol}`)?.scrollIntoView({
                        behavior: "smooth",
                        block: "center"
                      });
                    }}
                  >
                    <span>
                      <strong>{event.title}</strong>
                      <small>{event.detail}</small>
                    </span>
                    <time>{timeText(event.createdAt)}</time>
                  </button>
                ))
              ) : (
                <div className="fs-alarm-empty">暂无报警记录。</div>
              )}
            </div>
          </div>
        ) : null}
      </section>

      {signalRankingBoard}

      <section className="fs-funding-cap-watch">
        <div className="fs-funding-cap-watch-head">
          <div>
            <Space size={8} wrap>
              <span className="fs-section-order">2</span>
              <Typography.Text strong>规则变动提醒</Typography.Text>
              <Tag color={fundingCapWatch.data?.monitoring ? "green" : "default"}>
                {fundingCapWatch.data?.monitoring ? `监控 ${fundingCapWatch.data.itemCount} 币` : "未监控"}
              </Tag>
              {fundingCapWatch.data?.scan.running ? <Tag color="processing">检查中</Tag> : null}
            </Space>
          </div>
          <Space wrap>
            <Select
              className="fs-funding-cap-watch-exchanges"
              mode="multiple"
              allowClear
              maxTagCount={2}
              placeholder="选择交易所"
              options={fundingCapExchangeOptions}
              value={fundingCapWatchExchanges}
              onChange={(values: CryptoExchange[]) => setFundingCapWatchExchanges(values)}
            />
            <Input.Search
              className="fs-funding-cap-watch-input"
              allowClear
              placeholder="输入币种，如 TLM"
              value={fundingCapWatchInput}
              enterButton="添加监控"
              loading={addFundingCapWatch.isPending}
              onChange={(event) => setFundingCapWatchInput(event.target.value.toUpperCase())}
              onSearch={submitFundingCapWatch}
            />
            <Button
              icon={<ReloadOutlined />}
              disabled={!fundingCapWatch.data?.monitoring}
              loading={refreshFundingCapWatch.isPending}
              onClick={() => refreshFundingCapWatch.mutate()}
            >
              立即检查
            </Button>
          </Space>
        </div>
        {fundingCapWatch.isError ? (
          <Alert type="error" showIcon message="资金费规则监控读取失败" description={String(fundingCapWatch.error)} />
        ) : null}
        {fundingCapWatch.data?.items.length ? (
          <div className="fs-funding-cap-watch-list">
            {fundingCapWatch.data.items.map((item) => {
              return (
                <div className="fs-funding-cap-watch-item" key={item.id}>
                  <div className="fs-funding-cap-watch-symbol">
                    <Typography.Text strong>{item.symbol}</Typography.Text>
                    <Typography.Text type="secondary">更新 {timeText(item.lastCheckedAt)}</Typography.Text>
                  </div>
                  <Space size={6} wrap className="fs-funding-cap-watch-routes">
                    {item.exchanges.length ? (
                      item.exchanges.map((snapshot) => (
                        <Tooltip title={snapshot.message ?? "等待首次检查"} key={snapshot.exchange}>
                          <Tag color={fundingCapStatusColor(snapshot)}>
                            {snapshot.exchangeName}{" "}
                            {fundingRuleSnapshotText(snapshot)}
                          </Tag>
                        </Tooltip>
                      ))
                    ) : (
                      <Typography.Text type="secondary">
                        已选 {item.selectedExchanges.map((exchange) => exchangeLabel[exchange] ?? exchange).join("、")}，等待首次检查
                      </Typography.Text>
                    )}
                  </Space>
                  <Space size={6}>
                    <Button
                      size="small"
                      icon={<EditOutlined />}
                      onClick={() =>
                        setFundingCapExchangeEditor({
                          symbol: item.symbol,
                          exchanges: item.selectedExchanges
                        })
                      }
                    >
                      交易所
                    </Button>
                    <Popconfirm
                      title={`停止监控 ${item.symbol}？`}
                      okText="停止"
                      cancelText="取消"
                      onConfirm={() => deleteFundingCapWatch.mutate(item.symbol)}
                    >
                      <Button
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        loading={deleteFundingCapWatch.isPending && deleteFundingCapWatch.variables === item.symbol}
                      >
                        停止
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="fs-funding-cap-watch-empty">
            未添加币种：不检查交易所，不记录变化，也不发送提醒。
          </div>
        )}
        {fundingCapWatch.data?.recentEvents.length ? (
          <div className="fs-funding-cap-watch-events">
            <Typography.Text type="secondary">最近变化</Typography.Text>
            <Space size={6} wrap>
              {fundingCapWatch.data.recentEvents.slice(0, 4).map((event) => (
                <Tag color={event.pushed ? "green" : "gold"} key={event.id}>
                  {event.symbol} · {event.exchangeName} · {fundingRuleEventText(event)}
                </Tag>
              ))}
            </Space>
          </div>
        ) : null}
        <TransferStatusWatch />
      </section>

      <section className="fs-funding-formation">
        <div className="fs-funding-formation-head">
          <div>
            <Space size={8} wrap>
              <span className="fs-section-order">3</span>
              <Typography.Text strong>资金费形成</Typography.Text>
              <Tag color="blue">按交易所公式反推</Tag>
              <Tag
                color={
                  fundingCloudStatus.data?.status === "ok"
                    ? "cyan"
                    : fundingCloudStatus.isError || fundingCloudStatus.data?.status === "error"
                      ? "red"
                      : "gold"
                }
                title={fundingCloudStatus.data?.error || "本地仅展示，计算与预测日志位于腾讯云"}
              >
                {fundingCloudStatus.data?.status === "ok"
                  ? "腾讯云计算"
                  : fundingCloudStatus.isError || fundingCloudStatus.data?.status === "error"
                    ? "腾讯云不可用"
                    : "腾讯云连接中"}
              </Tag>
              <Tag color={fundingFormationMonitors.length ? "green" : "default"}>
                监控 {fundingFormationMonitors.length} 组
              </Tag>
            </Space>
          </div>
          <Space wrap className="fs-funding-formation-controls">
            <Select
              className="fs-funding-formation-exchange"
              size="small"
              mode="multiple"
              maxTagCount={2}
              allowClear
              placeholder="选择交易所"
              value={fundingFormationExchanges}
              options={defaultExchangeOptions}
              onChange={(values: CryptoExchange[]) => setFundingFormationExchanges(values)}
            />
            <Input
              className="fs-funding-formation-symbol"
              size="small"
              value={fundingFormationSymbolInput}
              placeholder="币种，可多个：BTC ETH"
              onChange={(event) => setFundingFormationSymbolInput(event.target.value.toUpperCase())}
              onPressEnter={submitFundingFormation}
            />
            <Input
              className="fs-funding-formation-target"
              size="small"
              value={fundingFormationTargetInput}
              placeholder="自定义目标，可空"
              addonAfter="%"
              onChange={(event) => setFundingFormationTargetInput(event.target.value)}
              onPressEnter={submitFundingFormation}
            />
            <Button type="primary" size="small" onClick={submitFundingFormation}
              loading={saveFundingWatch.isPending}
              disabled={!fundingWatch.data || fundingWatch.isError}>
              添加监控
            </Button>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              disabled={!fundingFormationMonitors.length}
              onClick={() => fundingFormationBatch.refetch()}
              loading={fundingFormationBatch.isFetching}
            >
              全部刷新
            </Button>
            <Button
              size="small"
              icon={<HistoryOutlined />}
              onClick={() => setFundingPredictionReviewOpen(true)}
            >
              预测复盘
            </Button>
          </Space>
        </div>

        {fundingWatch.isError ? <Alert type="warning" showIcon message="监控名单读取失败"
          description="无法确认已保存的监控名单，请刷新后重试。"
          action={<Button size="small" icon={<ReloadOutlined />} onClick={() => fundingWatch.refetch()}>重试</Button>} /> : null}
        {fundingFormationMonitors.length ? (
          <>
          <Space wrap style={{ padding: "8px 12px" }}>
            {(fundingWatch.data?.items ?? []).map((watch) => (
              <Tooltip key={`${watch.exchange}:${watch.symbol}`} title={
                <div>
                  <div>最近采集成功：{timeText(watch.health?.lastSuccessfulAt)}</div>
                  <div>最近检查：{timeText(watch.lastCheckedAt)}</div>
                  <div>最近记录：{timeText(watch.health?.lastRecordedAt)}</div>
                  {watch.health?.error ? <div>{watch.health.error}</div> : null}
                </div>
              }>
                <Tag color={["error", "stale", "missed"].includes(watch.health?.status ?? "") ? "orange" : "blue"}>
                  {watch.symbol} · {exchangeLabel[watch.exchange]}：{watch.health?.message ?? "等待状态更新"}
                  {watch.health?.nextCheckpointAt ? `；下次 ${timeText(watch.health.nextCheckpointAt)}（提前 ${watch.health.nextCheckpointMinutes} 分钟）` : ""}
                </Tag>
              </Tooltip>
            ))}
          </Space>
          <div className="fs-funding-formation-table-wrap">
            <table className="fs-funding-formation-table">
              <thead>
                <tr>
                  <th>组合</th>
                  <th>
                    <Tooltip title="交易所接口当前返回的下一次结算预估值，会随本周期溢价变化">
                      <span>当前资金费</span>
                    </Tooltip>
                  </th>
                  <th>
                    <Tooltip title="无上限观察值：按已形成溢价和最新溢价外推，不应用交易所最大资金费上限；实际结算仍受上限约束">
                      <span>预测资金费</span>
                    </Tooltip>
                  </th>
                  <th>溢价均值 / 最近分钟值</th>
                  <th>周期 / 距结算</th>
                  <th>上限</th>
                  <th>触达所需未来溢价</th>
                  <th>覆盖</th>
                  <th className="fs-formation-actions-column" aria-label="操作" />
                </tr>
              </thead>
              <tbody>
                {fundingFormationMonitors.map((monitor) => {
                  const result = fundingFormationResultsById.get(monitor.id);
                  const rawData = result?.data;
                  const snapshotTime = rawData?.updatedAt ? Date.parse(rawData.updatedAt) : NaN;
                  const predictionExpired = fundingFormationBatch.isError || !Number.isFinite(snapshotTime) || Date.now() - snapshotTime > 180000;
                  const data = rawData && predictionExpired ? {
                    ...rawData, stale: true, predictedFundingRate: null,
                    predictionSensitivityLow: null, predictionSensitivityHigh: null,
                    predictionMessage: "快照已过期或刷新失败，暂停展示预测，等待恢复。"
                  } : rawData;
                  const directionalTarget = data?.targets[0];
                  const periodMismatch = fundingFormationPeriodMismatchIds.has(monitor.id);
                  return (
                    <tr
                      key={monitor.id}
                      className={fundingFormationChartMonitor?.symbol === monitor.symbol ? "is-chart-selected" : undefined}
                      title="点击查看该币种在所有已选交易所的本周期溢价均值"
                      tabIndex={0}
                      onClick={() => setFundingFormationChartId(monitor.id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setFundingFormationChartId(monitor.id);
                        }
                      }}
                    >
                      <td>
                        <div className="fs-formation-combo">
                          <strong>{monitor.symbol}</strong>
                          <span>{exchangeLabel[monitor.exchange] ?? monitor.exchange}</span>
                          {typeof monitor.targetRate === "number" ? (
                            <small>目标 {rateText(monitor.targetRate)}</small>
                          ) : null}
                          {data?.ruleSourceUrl ? (
                            <Typography.Link href={data.ruleSourceUrl} target="_blank">
                              规则
                            </Typography.Link>
                          ) : null}
                        </div>
                      </td>
                      {result?.status === "error" ? (
                        <td colSpan={7}>
                          <Typography.Text type="danger">读取失败：{result.error}</Typography.Text>
                        </td>
                      ) : data ? (
                        <>
                          <td className="is-primary">{rateText(data.currentFundingRate)}</td>
                          <td>
                            <Tooltip title={data.predictionMessage}>
                              <div className="fs-formation-prediction">
                                <strong>{rateText(data.predictedFundingRate)}</strong>
                                <span>
                                  {data.stale ? "刷新失败 · 预测暂停"
                                    : data.predictionStatus === "insufficient" ? "样本不足或过期 · 暂不预测"
                                    : data.predictionStatus === "settled" ? "已结束 · 无上限观察"
                                    : data.predictionConfidence === "scenario" ? "远期情景 · 无上限观察"
                                    : data.predictionConfidence === "low" ? "低可信估算 · 无上限观察"
                                    : "临近结算估算 · 无上限观察"}
                                </span>
                                {typeof data.predictionSensitivityLow === "number" && typeof data.predictionSensitivityHigh === "number" ? (
                                  <Tooltip title={`按近期溢价高低值和历史分钟采样误差推算，不是置信区间，也不保证实际结算落在区间内。五分钟中位数对照预测：${rateText(data.robustPredictedFundingRate ?? null)}，尚未替代主预测。`}>
                                    <small>情景范围 {rateText(data.predictionSensitivityLow)} ～ {rateText(data.predictionSensitivityHigh)}</small>
                                  </Tooltip>
                                ) : null}
                              </div>
                            </Tooltip>
                          </td>
                          <td>
                            <Tooltip title={`最近一根1分钟溢价K线的收盘字段，进行中的K线还会变化；不是逐秒实时值。数据快照：${timeText(data.updatedAt)}。完整分钟中位数：${rateText(data.recentPremiumMedianRate ?? null)}。`}>
                              <div>
                                <div className="fs-formation-dual">
                                  <strong>{rateText(data.averagePremiumRate)}</strong>
                                  <span>{rateText(data.latestPremiumRate)}</span>
                                </div>
                                <small>{data.stale ? "旧数据 · " : ""}分钟起点 {data.latestPremiumCandleTime ? timeText(data.latestPremiumCandleTime) : "未提供"}</small>
                              </div>
                            </Tooltip>
                          </td>
                          <td>
                            <Tooltip
                              title={
                                periodMismatch
                                  ? "该周期与同一交易所的其他监控币种不一致"
                                  : null
                              }
                            >
                              <div className={`fs-formation-dual fs-formation-period ${periodMismatch ? "is-mismatch" : ""}`}>
                              <strong>{fundingPeriodText(data.fundingIntervalHours)}</strong>
                              <span>{fundingCountdown(data.minutesToFunding)}</span>
                              </div>
                            </Tooltip>
                          </td>
                          <td>
                            <div className="fs-formation-limit">
                              <span className="is-cap">
                                {compactRatePercent(
                                  directionalFundingLimit(
                                    data.currentFundingRate,
                                    data.maxFundingRate,
                                  ),
                                )}
                              </span>
                            </div>
                          </td>
                          <td>
                            <div className="fs-formation-target-lines">
                              <Tooltip
                                title={fundingFormationTargetDetail(
                                  directionalTarget,
                                  data.accuracyMessage,
                                )}
                              >
                                <strong>
                                  {directionalTarget
                                    ? fundingFormationTargetShortText(directionalTarget)
                                    : "-"}
                                </strong>
                              </Tooltip>
                            </div>
                          </td>
                          <td>
                            <div className="fs-formation-coverage">
                              <strong>{fundingFormationCoverageText(data.coverage)}</strong>
                              <span>
                                {data.accuracyStatus === "insufficient"
                                  ? "历史不足"
                                  : data.accuracyStatus === "estimated"
                                    ? "估算"
                                    : timeText(data.updatedAt)}
                              </span>
                            </div>
                          </td>
                        </>
                      ) : (
                        <td colSpan={7}>
                          <Typography.Text type="secondary">
                            {fundingFormationBatch.isFetching ? "读取中…" : "等待刷新"}
                          </Typography.Text>
                        </td>
                      )}
                      <td className="fs-formation-actions-column" onClick={(event) => event.stopPropagation()}>
                        <Space size={2} className="fs-formation-row-actions">
                          <Tooltip title="刷新">
                            <Button
                              type="text"
                              size="small"
                              aria-label={`刷新 ${monitor.symbol}`}
                              icon={<ReloadOutlined />}
                              onClick={() => fundingFormationBatch.refetch()}
                              loading={fundingFormationBatch.isFetching}
                            />
                          </Tooltip>
                          <Tooltip title="停止监控">
                            <Button
                              type="text"
                              size="small"
                              danger
                              aria-label={`停止 ${monitor.symbol}`}
                              icon={<DeleteOutlined />}
                              onClick={() => removeFundingFormationMonitor(monitor.id)}
                              disabled={saveFundingWatch.isPending || fundingWatch.isError}
                            />
                          </Tooltip>
                        </Space>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="fs-funding-formation-table-note">
              <span>点击任一合约可在同一张图中比较该币种各交易所本周期形成的溢价均值；当前资金费是交易所接口实时预估。</span>
            </div>
          </div>
          {fundingFormationChartResults.length ? (
            <div className="fs-funding-premium-chart-panel">
              <div className="fs-funding-premium-chart-head">
                <Space size={7} wrap>
                  <Typography.Text strong>
                    {fundingFormationChartMonitor?.symbol} · 本周期溢价均值
                  </Typography.Text>
                  {fundingFormationChartResults.map((result) => (
                    <Tag key={result.exchange}>
                      {result.exchangeName} {fundingPeriodText(result.fundingIntervalHours)}
                    </Tag>
                  ))}
                </Space>
                <Typography.Text type="secondary">各交易所分别按自身当前结算周期计算</Typography.Text>
              </div>
              <div className="fs-funding-premium-chart-legend">
                {fundingFormationChartResults.map((result) => (
                  <span key={result.exchange}>
                    <i style={{ background: fundingFormationLineColor[result.exchange] ?? "#1677ff" }} />
                    {result.exchangeName}
                    <strong>{rateText(result.averagePremiumRate)}</strong>
                  </span>
                ))}
              </div>
              {fundingFormationPremiumChartOption ? (
                <AppChart
                  option={fundingFormationPremiumChartOption}
                  style={{ height: 230 }}
                  notMerge
                />
              ) : (
                <div className="fs-funding-premium-chart-empty">当前周期暂无可绘制的公开溢价历史。</div>
              )}
              <div className="fs-funding-premium-chart-source">
                曲线表示“截至该时点、按交易所公式权重形成的溢价均值”，不是即时溢价指数；仅显示各交易所当前周期。数据源：
                {fundingFormationChartResults.map((result) => `${result.exchangeName} ${result.historySource}`).join("；")}。
              </div>
            </div>
          ) : null}
          </>
        ) : (
          <div className="fs-funding-formation-empty">
            尚未添加监控。可输入多个币种并选择多个交易所，一次生成全部组合。
          </div>
        )}
      </section>

      <Modal
        title={
          <Space size={8}>
            <SettingOutlined />
            <span>Astro 行情订阅</span>
            <Tag color="blue">{astroSubscriptionDraft.length} 个行情源</Tag>
          </Space>
        }
        open={astroSubscriptionsOpen}
        onCancel={() => setAstroSubscriptionsOpen(false)}
        onOk={() => saveAstroSubscriptions.mutate({
          markets: astroSubscriptionDraft,
          minVolumeUsdt: astroMinVolumeDraft,
          blockedPairs: astroBlockedPairDraft,
          blockedCoins: astroScannerStatus?.blockedCoins ?? []
        })}
        okText="保存规则"
        cancelText="取消"
        okButtonProps={{ disabled: astroSubscriptionDraft.length < 2 }}
        confirmLoading={saveAstroSubscriptions.isPending}
        width={820}
      >
        <div className="astro-subscription-summary">
          <Typography.Text strong>Pulse 可用行情</Typography.Text>
          <Typography.Text type="secondary">
            按现货/合约独立订阅，保存后下一轮 {astroScannerStatus?.intervalSeconds ?? 10} 秒扫描立即生效。
          </Typography.Text>
        </div>
        {astroScannerStatus?.lastError ? (
          <Alert type="error" showIcon message="Pulse 扫描异常" description={astroScannerStatus.lastError} />
        ) : null}
        <div className="astro-filter-rule-panel">
          <div className="astro-filter-rule-row">
            <div className="astro-filter-rule-copy">
              <Typography.Text strong>24 小时成交额门槛</Typography.Text>
              <Typography.Text type="secondary">
                任一腿已公开的成交额低于门槛时，不参与差价扫描。
              </Typography.Text>
            </div>
            <InputNumber
              min={0}
              step={10000}
              value={astroMinVolumeDraft}
              onChange={(value) => setAstroMinVolumeDraft(Number(value ?? 0))}
              addonAfter="USDT"
              className="astro-volume-threshold-input"
            />
          </div>
          <div className="astro-block-pair-editor">
            <Select
              value={astroBlockMarketDraft || undefined}
              placeholder="选择交易所行情"
              options={(astroScannerStatus?.availableMarkets ?? []).map((market) => ({
                value: market.key,
                label: `${market.exchangeName} ${market.marketType === "spot" ? "现货" : "合约"}`
              }))}
              onChange={setAstroBlockMarketDraft}
            />
            <Input
              value={astroBlockSymbolDraft}
              placeholder="过滤币种，如 BTC"
              onChange={(event) => setAstroBlockSymbolDraft(event.target.value)}
              onPressEnter={addAstroBlockedPair}
            />
            <Button onClick={addAstroBlockedPair}>添加过滤</Button>
          </div>
          <div className="astro-block-pair-list">
            {astroBlockedPairDraft.length ? astroBlockedPairDraft.map((item) => (
              <Tag
                key={`${item.marketKey}-${item.symbol}`}
                closable
                onClose={(event) => {
                  event.preventDefault();
                  setAstroBlockedPairDraft((current) => current.filter(
                    (rule) => rule.marketKey !== item.marketKey || rule.symbol !== item.symbol
                  ));
                }}
              >
                {astroMarketLabel(item.marketKey)} · {item.symbol.replace(/USDT$/, "")}
              </Tag>
            )) : (
              <Typography.Text type="secondary">暂未过滤交易对</Typography.Text>
            )}
          </div>
        </div>
        <div className="astro-subscription-grid">
          {astroMarketsByExchange.map(([exchangeName, markets]) => {
            const selectedCount = markets.filter((market) => astroSubscriptionDraft.includes(market.key)).length;
            return (
              <div
                className={`astro-subscription-card ${selectedCount ? "is-active" : ""}`}
                key={exchangeName}
              >
                <div className="astro-subscription-card-head">
                  <Typography.Text strong>{exchangeName}</Typography.Text>
                  <Typography.Text type="secondary">{selectedCount}/{markets.length}</Typography.Text>
                </div>
                <div className="astro-subscription-options">
                  {markets.map((market) => (
                    <Checkbox
                      key={market.key}
                      checked={astroSubscriptionDraft.includes(market.key)}
                      onChange={(event) => toggleAstroSubscription(market.key, event.target.checked)}
                    >
                      <span className={market.marketType === "spot" ? "astro-market-spot" : "astro-market-future"}>
                        {market.marketType === "spot" ? "现货" : "合约"}
                      </span>
                    </Checkbox>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
        <div className="astro-subscription-footnote">
          <Tag color="green">已订阅</Tag>
          <Typography.Text type="secondary">
            只有勾选且满足成交额门槛、未命中过滤规则的行情会参与 SF/FF 实时买一卖一差价计算。
          </Typography.Text>
        </div>
      </Modal>

      <Modal
        title="资金费预测复盘"
        open={fundingPredictionReviewOpen}
        onCancel={() => setFundingPredictionReviewOpen(false)}
        footer={
          <Button type="primary" onClick={() => setFundingPredictionReviewOpen(false)}>
            完成
          </Button>
        }
        width={1180}
        styles={{ body: { maxHeight: "75vh", overflowY: "auto" } }}
      >
        <div className="fs-prediction-review-toolbar">
          <Space size={8} wrap>
            <Select
              size="small"
              value={fundingPredictionReviewDays}
              options={[
                { value: 7, label: "近 7 天" },
                { value: 30, label: "近 30 天" },
                { value: 90, label: "近 90 天" },
                { value: 365, label: "近 1 年" }
              ]}
              onChange={setFundingPredictionReviewDays}
            />
            <Select size="small" style={{ minWidth: 140 }} value={fundingPredictionReviewModel}
              options={[{ value: "", label: "当前模型" }, { value: "all", label: "全部历史模型" }]}
              onChange={setFundingPredictionReviewModel}
            />
            <Select
              size="small"
              value={fundingPredictionReviewCheckpoint}
              options={[240, 180, 120, 60, 30, 15, 5].map((value) => ({
                value,
                label: `结算前 ${value} 分钟`
              }))}
              onChange={setFundingPredictionReviewCheckpoint}
            />
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={fundingPredictionReview.isFetching}
              onClick={() => fundingPredictionReview.refetch()}
            >
              刷新复盘
            </Button>
          </Space>
          <Typography.Text type="secondary">
            更新 {timeText(fundingPredictionReview.data?.updatedAt)}
          </Typography.Text>
        </div>
        {fundingPredictionReview.isError ? (
          <Alert
            type="error"
            showIcon
            message="预测复盘读取失败"
            description={String(fundingPredictionReview.error)}
          />
        ) : null}
        <Alert
          className="fs-prediction-review-rule"
          type={fundingPredictionReview.data?.sampleStatus === "ok" ? "info" : "warning"}
          showIcon
          message={fundingPredictionReview.data?.successDefinition ?? "固定检查点开始记录后，结算完成才计入成功率。"}
          description={`统计本次所选时段的全部匹配记录，仅将边界完整样本计入成功率；${fundingPredictionReviewModel === "all" ? "当前为跨模型历史汇总" : "当前模型"}。${fundingPredictionReview.data?.sampleStatus === "insufficient" ? "可评分样本少于 30 个，命中率仅供观察。" : ""}明细最多显示 ${fundingPredictionReview.data?.detailLimit ?? 100} 条，优先展示评分已修正的旧告警。`}

        />
        {(fundingPredictionReview.data?.correctedLogCount ?? 0) > 0 ? (
          <Alert type="info" showIcon style={{ marginBottom: 8 }}
            message={`${fundingPredictionReview.data?.correctedLogCount} 条旧日志的评价已修正`}
            description="原始日志保留；下表并列显示旧评价、折算值与当前结果，成功率使用当前评分。" />
        ) : null}
        {(fundingPredictionReview.data?.legacyCount ?? 0) > 0 ? (
          <Alert type="warning" showIcon style={{ marginBottom: 8 }}
            message={`${fundingPredictionReview.data?.legacyCount} 条历史记录缺少完整有效的上下限，未计入成功率`}
            description={`旧口径命中率：${fundingPredictionReview.data?.legacyHitRate == null ? "-" : `${(fundingPredictionReview.data.legacyHitRate * 100).toFixed(1)}%`}，仅单列参考。`} />
        ) : null}
        <div className="fs-prediction-review-metrics">
          <div>
            <span>可评分 / 已结算</span>
            <strong>{fundingPredictionReview.data?.scoredCount ?? 0} / {fundingPredictionReview.data?.settledCount ?? 0}</strong>
          </div>
          <div>
            <span>成功率</span>
            <strong>
              {fundingPredictionReview.data?.hitRate == null
                ? "-"
                : `${(fundingPredictionReview.data.hitRate * 100).toFixed(1)}%`}
            </strong>
          </div>
          <div>
            <span>平均绝对误差</span>
            <strong>{compactRatePercent(fundingPredictionReview.data?.meanAbsoluteError)}</strong>
          </div>
          <div>
            <span>方向命中率</span>
            <strong>
              {fundingPredictionReview.data?.directionHitRate == null
                ? "-"
                : `${(fundingPredictionReview.data.directionHitRate * 100).toFixed(1)}%`}
            </strong>
          </div>
          <div>
            <span>待结算</span>
            <strong>{fundingPredictionReview.data?.pendingCount ?? 0}</strong>
          </div>
        </div>
        <Table<CryptoFundingPredictionReviewItem>
          className="fs-prediction-review-table"
          size="small"
          rowKey="id"
          loading={fundingPredictionReview.isLoading || fundingPredictionReview.isFetching}
          dataSource={fundingPredictionReview.data?.items ?? []}
          columns={[
            {
              title: "结算时间",
              dataIndex: "settlementTime",
              width: 128,
              render: (value: string) => timeText(value)
            },
            {
              title: "组合",
              width: 110,
              render: (_, row) => `${row.symbol} · ${exchangeLabel[row.exchange] ?? row.exchange}`
            },
            {
              title: "提前量",
              dataIndex: "checkpointMinutes",
              width: 76,
              render: (value: number) => `${value} 分钟`
            },
            {
              title: "系统预测",
              dataIndex: "systemPredictedRate",
              width: 92,
              render: (value: number) => rateText(value)
            },
            {
              title: "结算折算值",
              dataIndex: "settlementPredictedRate",
              width: 110,
              render: (value: number, row) => <Space direction="vertical" size={0}>
                <span>{rateText(value)}</span>
                {row.boundsStatus !== "complete" ? <Tag color="gold">边界缺失或无效</Tag> : null}
              </Space>
            },
            {
              title: "交易所预测",
              dataIndex: "exchangePredictedRate",
              width: 96,
              render: (value: number | null) => rateText(value)
            },
            {
              title: "实际结算",
              dataIndex: "actualFundingRate",
              width: 92,
              render: (value: number | null) => rateText(value)
            },
            {
              title: "绝对误差",
              dataIndex: "absoluteError",
              width: 88,
              render: (value: number | null) => compactRatePercent(value)
            },
            {
              title: "结果",
              width: 180,
              render: (_, row) => {
                if (row.evaluationStatus === "pending") return <Tag color="gold">待结算</Tag>;
                if (row.evaluationStatus === "unavailable") return <Tag>缺数据</Tag>;
                return <Space direction="vertical" size={0}>
                  <Tag color={row.success ? "green" : "red"}>{row.success ? "命中" : "未命中"}{!row.includedInAccuracy ? "（旧口径）" : ""}</Tag>
                  {row.scoreCorrected ? <Tooltip title={`${row.originalEvaluation?.message}；原日志时间 ${timeText(row.originalEvaluation?.at)}`}>
                    <Typography.Text type="secondary">旧评价：{row.originalEvaluation?.success ? "命中" : "未命中"} → 已修正</Typography.Text>
                  </Tooltip> : null}
                </Space>;
              }
            }
          ] as ColumnsType<CryptoFundingPredictionReviewItem>}
          pagination={{ pageSize: 8, size: "small", hideOnSinglePage: true }}
          scroll={{ x: 1020 }}
        />
      </Modal>

      <Modal
        title="交易监控 · 声音报警规则"
        open={alarmSettingsOpen}
        onCancel={() => setAlarmSettingsOpen(false)}
        footer={
          <Button type="primary" onClick={() => setAlarmSettingsOpen(false)}>
            完成
          </Button>
        }
        width={640}
      >
        <Alert
          type="info"
          showIcon
          message="修改后自动保存"
          description="报警只在状态刷新后触发，不会自动下单。关闭总开关即为“无报警”。"
        />
        <div className="fs-alarm-settings">
          <div className="fs-alarm-setting-row is-master">
            <div>
              <strong>声音报警总开关</strong>
              <span>{alarmSettings.enabled ? "声音和弹窗均已开启" : "无报警，不播放声音也不弹窗"}</span>
            </div>
            <Switch
              checked={alarmSettings.enabled}
              checkedChildren="报警开"
              unCheckedChildren="无报警"
              onChange={(enabled) => {
                if (enabled) requestDesktopAlarmPermission();
                setAlarmSettings((current) => ({ ...current, enabled }));
              }}
            />
          </div>
          <div className="fs-alarm-setting-row">
            <div>
              <strong>报警声音</strong>
              <span>触发规则时播放四段高响度双音提示</span>
            </div>
            <Switch
              checked={alarmSettings.soundEnabled}
              disabled={!alarmSettings.enabled}
              onChange={(soundEnabled) => setAlarmSettings((current) => ({ ...current, soundEnabled }))}
            />
          </div>
          <div className="fs-alarm-volume-row">
            <div>
              <strong>报警音量</strong>
              <span>{alarmSettings.volumePercent}%</span>
            </div>
            <Slider
              min={20}
              max={150}
              step={10}
              value={alarmSettings.volumePercent}
              disabled={!alarmSettings.enabled || !alarmSettings.soundEnabled}
              marks={{ 20: "20", 100: "100", 150: "150" }}
              onChange={(volumePercent) =>
                setAlarmSettings((current) => ({ ...current, volumePercent }))
              }
            />
          </div>
          <div className="fs-alarm-setting-row">
            <div>
              <strong>有可借 B 且可开单</strong>
              <span>可借状态从无变有，并同时满足系统开单条件</span>
            </div>
            <Switch
              checked={alarmSettings.borrowableOpenEnabled}
              disabled={!alarmSettings.enabled}
              onChange={(borrowableOpenEnabled) =>
                setAlarmSettings((current) => ({ ...current, borrowableOpenEnabled }))
              }
            />
          </div>
          <div className="fs-alarm-setting-row">
            <div>
              <strong>机会状态发生变化</strong>
              <span>监控可借平台、可开单状态、观察状态和机会移出</span>
            </div>
            <Switch
              checked={alarmSettings.changeEnabled}
              disabled={!alarmSettings.enabled}
              onChange={(changeEnabled) => setAlarmSettings((current) => ({ ...current, changeEnabled }))}
            />
          </div>
          <div className="fs-alarm-setting-row">
            <div>
              <strong>自定义数值规则</strong>
              <span>三项条件同时满足并且有可借 B 时报警</span>
            </div>
            <Switch
              checked={alarmSettings.customEnabled}
              disabled={!alarmSettings.enabled}
              onChange={(customEnabled) => setAlarmSettings((current) => ({ ...current, customEnabled }))}
            />
          </div>
          <div className={`fs-alarm-custom-grid ${alarmSettings.customEnabled ? "" : "is-disabled"}`}>
            <label>
              <span>日化资金费 ≤</span>
              <InputNumber
                value={alarmSettings.maxDailyFundingPercent}
                min={-100}
                max={100}
                step={0.1}
                precision={2}
                addonAfter="%"
                disabled={!alarmSettings.enabled || !alarmSettings.customEnabled}
                onChange={(value) =>
                  setAlarmSettings((current) => ({
                    ...current,
                    maxDailyFundingPercent: typeof value === "number" ? value : current.maxDailyFundingPercent
                  }))
                }
              />
            </label>
            <label>
              <span>开仓差价 ≥</span>
              <InputNumber
                value={alarmSettings.minOpenSpreadPercent}
                min={-100}
                max={100}
                step={0.1}
                precision={2}
                addonAfter="%"
                disabled={!alarmSettings.enabled || !alarmSettings.customEnabled}
                onChange={(value) =>
                  setAlarmSettings((current) => ({
                    ...current,
                    minOpenSpreadPercent: typeof value === "number" ? value : current.minOpenSpreadPercent
                  }))
                }
              />
            </label>
            <label>
              <span>净收益/期 ≥</span>
              <InputNumber
                value={alarmSettings.minNetFundingPercent}
                min={-100}
                max={100}
                step={0.01}
                precision={3}
                addonAfter="%"
                disabled={!alarmSettings.enabled || !alarmSettings.customEnabled}
                onChange={(value) =>
                  setAlarmSettings((current) => ({
                    ...current,
                    minNetFundingPercent: typeof value === "number" ? value : current.minNetFundingPercent
                  }))
                }
              />
            </label>
          </div>
          <div className="fs-alarm-setting-row">
            <div>
              <strong>同类报警冷却</strong>
              <span>避免状态抖动时连续弹窗</span>
            </div>
            <InputNumber
              value={alarmSettings.cooldownSeconds}
              min={10}
              max={3600}
              step={10}
              addonAfter="秒"
              onChange={(value) =>
                setAlarmSettings((current) => ({
                  ...current,
                  cooldownSeconds: typeof value === "number" ? value : current.cooldownSeconds
                }))
              }
            />
          </div>
          <Button block icon={<SoundOutlined />} onClick={testAlarm}>
            测试声音和弹窗
          </Button>
        </div>
      </Modal>

      <Modal
        title={fundingCapExchangeEditor ? `${fundingCapExchangeEditor.symbol} · 选择监控交易所` : "选择监控交易所"}
        open={Boolean(fundingCapExchangeEditor)}
        okText="保存"
        cancelText="取消"
        confirmLoading={updateFundingCapWatch.isPending}
        okButtonProps={{ disabled: !fundingCapExchangeEditor?.exchanges.length }}
        onCancel={() => setFundingCapExchangeEditor(null)}
        onOk={() => {
          if (!fundingCapExchangeEditor?.exchanges.length) {
            message.warning("请至少选择一个交易所");
            return;
          }
          updateFundingCapWatch.mutate({
            symbol: fundingCapExchangeEditor.symbol,
            exchanges: fundingCapExchangeEditor.exchanges
          });
        }}
      >
        <Typography.Paragraph type="secondary">
          只检查选中的交易所；最大资金费上限变化时提醒，结算周期变化仅记录、不推送。
        </Typography.Paragraph>
        <Select
          className="fs-funding-cap-watch-editor"
          mode="multiple"
          allowClear
          placeholder="至少选择一个交易所"
          options={fundingCapExchangeOptions}
          value={fundingCapExchangeEditor?.exchanges ?? []}
          onChange={(values: CryptoExchange[]) =>
            setFundingCapExchangeEditor((previous) =>
              previous ? { ...previous, exchanges: values } : previous
            )
          }
        />
      </Modal>

      <Modal
        title={editingMappingId ? "编辑币名映射" : "添加币名映射"}
        open={mappingOpen}
        onCancel={() => {
          setMappingOpen(false);
          resetMappingDraft();
        }}
        footer={null}
        width={760}
      >
        {fsSettings.isError ? <Alert type="error" showIcon message="映射读取失败" description={String(fsSettings.error)} /> : null}
        {mappingScanResult ? (
          <Alert
            className="fs-mapping-scan-alert"
            type={mappingScanResult.errorCount ? "warning" : "success"}
            showIcon
            message={`全市场扫描完成：别名 ${mappingScanResult.aliasCandidateCount}，面值 ${mappingScanResult.faceValueCandidateCount}，新增 ${mappingScanResult.createdCount}，更新 ${mappingScanResult.updatedCount}，已存在 ${mappingScanResult.unchangedCount}`}
            description={
              mappingScanResult.errorCount
                ? `部分交易所读取失败 ${mappingScanResult.errorCount} 个，已跳过；可稍后重扫。`
                : `已扫描 ${mappingScanResult.exchangeCount} 个交易所、${mappingScanResult.symbolCount} 个币种；核对 ${mappingScanResult.aliasScannedCount} 个单所合约的指数成分。`
            }
          />
        ) : null}
        <div className="fs-mapping-form">
          <label>
            <span>交易所</span>
            <Select
              placeholder="选择交易所"
              value={mappingDraft.exchange ?? undefined}
              options={exchangeOptions}
              onChange={(value: CryptoExchange) => setMappingDraft((previous) => ({ ...previous, exchange: value }))}
            />
          </label>
          <label>
            <span>类型</span>
            <Select
              value={mappingDraft.marketType}
              options={marketTypeOptions}
              onChange={(value: CryptoMarketType) => setMappingDraft((previous) => ({ ...previous, marketType: value }))}
            />
          </label>
          <label>
            <span>原始名</span>
            <Input
              placeholder="例如: RATS"
              value={mappingDraft.inputSymbol}
              onChange={(event) => setMappingDraft((previous) => ({ ...previous, inputSymbol: event.target.value.toUpperCase() }))}
            />
          </label>
          <label>
            <span>映射名</span>
            <Input
              placeholder="例如: 1000RATS"
              value={mappingDraft.mappedSymbol}
              onChange={(event) => setMappingDraft((previous) => ({ ...previous, mappedSymbol: event.target.value.toUpperCase() }))}
            />
          </label>
          <label>
            <span>价格汇率（映射单价 / 原始单价）</span>
            <Select
              value={mappingDraft.priceRatio}
              options={priceRatioOptions}
              onChange={(value: number) => setMappingDraft((previous) => ({ ...previous, priceRatio: value }))}
            />
          </label>
          <label>
            <span>备注</span>
            <Input
              placeholder="可选"
              value={mappingDraft.note}
              onChange={(event) => setMappingDraft((previous) => ({ ...previous, note: event.target.value }))}
            />
          </label>
          <div className="fs-mapping-actions">
            <Button onClick={() => scanMappings.mutate()} loading={scanMappings.isPending}>
              扫描面值与别名
            </Button>
            {editingMappingId ? <Button onClick={resetMappingDraft}>取消编辑</Button> : null}
            <Button type="primary" onClick={saveMapping} loading={savingMapping}>
              {editingMappingId ? "保存" : "提交"}
            </Button>
          </div>
        </div>
        <Table
          className="fs-mapping-table"
          size="small"
          rowKey="id"
          loading={fsSettings.isLoading || fsSettings.isFetching}
          columns={mappingColumns}
          dataSource={fsSettings.data?.symbolMappings ?? []}
          pagination={{ pageSize: 6, size: "small" }}
          scroll={{ x: 820 }}
        />
      </Modal>
    </div>
  );
}
