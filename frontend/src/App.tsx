import {
  BellOutlined,
  ExclamationCircleOutlined,
  FundOutlined,
  HomeOutlined,
  LineChartOutlined,
  ReadOutlined,
  RobotOutlined
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Layout, Menu, Modal, Space, Spin, Tag, Typography } from "antd";
import type { MenuProps } from "antd";
import { Navigate, Route, RouterProvider, Routes, createBrowserRouter, useLocation, useNavigate } from "react-router-dom";
import type { CryptoFsSignal, CryptoFsSignalCheck } from "./api";
import { cryptoApi } from "./api/crypto";

const { Sider, Content } = Layout;

const FsPage = lazy(() => import("./pages/FsPage"));
const AstroDexMappingsPage = lazy(() => import("./pages/AstroDexMappingsPage"));
const AstroScanRulesPage = lazy(() => import("./pages/AstroScanRulesPage"));
const AstroAutoCardLayout = lazy(() => import("./pages/AstroAutoCardLayout"));
const AstroStatusPage = lazy(() => import("./pages/AstroStatusPage"));
const DexHistoryPage = lazy(() => import("./pages/DexHistoryPage"));
const LocalExchangeNewsPage = lazy(() => import("./pages/LocalExchangeNewsPage"));

const navItems = [
  { key: "/exchange-announcements", icon: <ReadOutlined />, label: "交易所新闻" },
  { key: "/fs", icon: <FundOutlined />, label: "交易监控" },
  { key: "/astro", icon: <RobotOutlined />, label: "Astro 建卡" },
  { key: "/dex-history", icon: <LineChartOutlined />, label: "DEX历史差价" }
];

function PageFallback() {
  return (
    <div className="page-loading">
      <Spin size="small" />
      <Typography.Text type="secondary">加载中</Typography.Text>
    </div>
  );
}

function LegacyAstroRulesRedirect() {
  const location = useLocation();
  return <Navigate to={location.hash.includes("okxdex-mappings") ? "/astro/dex" : `/astro/rules${location.search}${location.hash}`} replace />;
}

const DEX_ALERT_SEEN_KEY = "astro-okxdex-unmapped-alerts-v1";
const BORROW_ALERT_SEEN_KEY = "fs-global-borrow-alerts-v1";
const FS_ALARM_SETTINGS_KEY = "fs-alarm-settings-v1";

const borrowExchangeLabels: Record<string, string> = {
  bn: "Binance",
  by: "Bybit",
  bg: "Bitget",
  gt: "Gate",
  okx: "OKX",
  as: "Aster"
};

function loadSeenDexAlerts(): Set<string> {
  try {
    const values = JSON.parse(window.sessionStorage.getItem(DEX_ALERT_SEEN_KEY) ?? "[]");
    return new Set(Array.isArray(values) ? values.map(String) : []);
  } catch {
    return new Set();
  }
}

function dexAlertKey(item: { exchange?: string; symbol: string; chainIndex: string; contractAddress: string }) {
  return `${item.exchange ?? "okxdex"}:${item.symbol}:${item.chainIndex}:${item.contractAddress}`;
}

function loadSeenBorrowAlerts(): Set<string> {
  try {
    const values = JSON.parse(window.sessionStorage.getItem(BORROW_ALERT_SEEN_KEY) ?? "[]");
    return new Set(Array.isArray(values) ? values.map(String) : []);
  } catch {
    return new Set();
  }
}

function borrowAlertEnabled() {
  try {
    const settings = JSON.parse(window.localStorage.getItem(FS_ALARM_SETTINGS_KEY) ?? "{}");
    return settings?.enabled !== false && settings?.borrowableOpenEnabled !== false;
  } catch {
    return true;
  }
}

function fsSignalChecks(signal: CryptoFsSignal): CryptoFsSignalCheck[] {
  return Object.values(signal.checks ?? {}).filter((check): check is CryptoFsSignalCheck => Boolean(check));
}

function fsSignalHasBorrowInventory(signal: CryptoFsSignal) {
  if (typeof signal.executableBorrow === "boolean") return signal.executableBorrow;
  return fsSignalChecks(signal).some((check) => check.canBorrow === true);
}

function borrowAlertKey(signal: CryptoFsSignal) {
  return `${signal.symbol}:${signal.futuresExchange}:${signal.spotExchange}`;
}

function percentText(value?: number | null) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "-";
  return `${value > 0 ? "+" : ""}${(value * 100).toFixed(3)}%`;
}

function DexMappingAlertMonitor() {
  const navigate = useNavigate();
  const [modal, contextHolder] = Modal.useModal();
  const seenRef = useRef<Set<string>>(loadSeenDexAlerts());
  const dialogOpenRef = useRef(false);
  const status = useQuery({
    queryKey: ["astro-auto-card-status"],
    queryFn: () => cryptoApi.astroAutoCardStatus(),
    refetchInterval: 5000,
    retry: false
  });
  const unmappedItems = status.data?.spreadScanner?.autoCardRules?.sf.okxDexRoute?.unmappedItems ?? [];

  useEffect(() => {
    const activeKeys = new Set(unmappedItems.map(dexAlertKey));
    let seenChanged = false;
    for (const key of Array.from(seenRef.current)) {
      if (!activeKeys.has(key)) {
        seenRef.current.delete(key);
        seenChanged = true;
      }
    }
    if (seenChanged) window.sessionStorage.setItem(DEX_ALERT_SEEN_KEY, JSON.stringify(Array.from(seenRef.current)));
    const newItems = unmappedItems.filter((item) => !seenRef.current.has(dexAlertKey(item)));
    if (!newItems.length || dialogOpenRef.current) return;

    for (const item of newItems) seenRef.current.add(dexAlertKey(item));
    window.sessionStorage.setItem(DEX_ALERT_SEEN_KEY, JSON.stringify(Array.from(seenRef.current)));
    dialogOpenRef.current = true;
    modal.confirm({
      title: (
        <Space>
          <ExclamationCircleOutlined style={{ color: "#d48806" }} />
          <span>发现待配置的 DEX 机会</span>
        </Space>
      ),
      content: (
        <div className="astro-dex-alert-dialog">
          <Typography.Paragraph>
            下列机会尚未确认已在 Astro 配置，因此已阻止自动建卡：
          </Typography.Paragraph>
          {newItems.slice(0, 6).map((item) => (
            <div className="astro-dex-alert-item" key={dexAlertKey(item)}>
              <div>
                <strong>{item.symbol}</strong> <Tag>{item.exchange === "pancakeswapv3" ? "PancakeSwap V3" : "OKXDEX"}</Tag>
                <Tag color="blue">{item.chainLabel || item.chainIndex}</Tag>
                {item.maxOpenSpreadPct == null ? null : <Tag color="gold">差价 {item.maxOpenSpreadPct.toFixed(2)}%</Tag>}
              </div>
              <Typography.Text type="secondary">
                目标：{item.targetExchanges.map((exchange) => exchange.toUpperCase()).join(" / ") || "待确认"}
              </Typography.Text>
            </div>
          ))}
          {newItems.length > 6 ? <Typography.Text type="secondary">另有 {newItems.length - 6} 个待配置币种</Typography.Text> : null}
          <Typography.Paragraph type="secondary" className="astro-dex-alert-help">
            请先在 Astro 配置对应 DEX 的币种，再确认交易所、链和合约地址。
          </Typography.Paragraph>
        </div>
      ),
      okText: "去配置",
      cancelText: "稍后处理",
      centered: true,
      width: 560,
      onOk: () => navigate("/astro/dex"),
      afterClose: () => {
        dialogOpenRef.current = false;
      }
    });
  }, [modal, navigate, unmappedItems]);

  return contextHolder;
}

function BorrowOpportunityAlertMonitor() {
  const navigate = useNavigate();
  const location = useLocation();
  const [modal, contextHolder] = Modal.useModal();
  const seenRef = useRef<Set<string>>(loadSeenBorrowAlerts());
  const dialogOpenRef = useRef(false);
  const signals = useQuery({
    queryKey: ["fs-signals", 20],
    queryFn: () => cryptoApi.fsSignals(20),
    refetchInterval: 10000,
    retry: false
  });
  const actionableItems = useMemo(
    () => (signals.data?.items ?? []).filter(
      (item) => item.actionable === true && fsSignalHasBorrowInventory(item)
    ),
    [signals.data?.items]
  );

  useEffect(() => {
    if (signals.data?.status !== "ok" || !borrowAlertEnabled()) return;
    const updatedAt = signals.data.updatedAt ? new Date(signals.data.updatedAt).getTime() : 0;
    if (!Number.isFinite(updatedAt) || Date.now() - updatedAt > 60_000) return;

    const activeKeys = new Set(actionableItems.map(borrowAlertKey));
    let seenChanged = false;
    for (const key of Array.from(seenRef.current)) {
      if (!activeKeys.has(key)) {
        seenRef.current.delete(key);
        seenChanged = true;
      }
    }
    const newItems = actionableItems.filter((item) => !seenRef.current.has(borrowAlertKey(item)));
    if (newItems.length) {
      for (const item of newItems) seenRef.current.add(borrowAlertKey(item));
      seenChanged = true;
    }
    if (seenChanged) {
      window.sessionStorage.setItem(BORROW_ALERT_SEEN_KEY, JSON.stringify(Array.from(seenRef.current)));
    }

    // 交易监控页自身已有声音和弹窗；这里只补齐离开该页面后的全局提醒。
    if (!newItems.length || location.pathname === "/fs" || dialogOpenRef.current) return;

    dialogOpenRef.current = true;
    const first = newItems[0];
    const focusBorrowMonitor = () => {
      navigate("/fs#borrow-monitor");
      window.setTimeout(() => {
        document.getElementById(`fs-signal-${first.symbol}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 700);
    };
    if ("Notification" in window && window.Notification.permission === "granted") {
      const notification = new window.Notification(`${first.symbol} 有可借 B，可开单`, {
        body: `开仓差价 ${percentText(first.openSpreadRate ?? first.spreadRate)} · 净收益/期 ${percentText(first.netFundingRate)}`,
        tag: "fs-global-borrow-opportunity"
      });
      notification.onclick = () => {
        window.focus();
        notification.close();
        focusBorrowMonitor();
      };
    }
    modal.confirm({
      title: (
        <Space>
          <BellOutlined style={{ color: "#1677ff" }} />
          <span>借币监控发现可开单机会</span>
        </Space>
      ),
      content: (
        <div className="fs-alarm-dialog-list">
          {newItems.slice(0, 5).map((item) => {
            const platforms = fsSignalChecks(item)
              .filter((check) => check.canBorrow === true)
              .map((check) => borrowExchangeLabels[check.exchange] ?? check.exchange);
            return (
              <div key={borrowAlertKey(item)}>
                <strong>{item.symbol} · {platforms.join(" / ") || "已确认可借"}</strong>
                <span>
                  开仓差价 {percentText(item.openSpreadRate ?? item.spreadRate)} · 日化 {percentText(item.dailyFundingRate)} · 净收益/期 {percentText(item.netFundingRate)}
                </span>
              </div>
            );
          })}
          {newItems.length > 5 ? <Typography.Text type="secondary">另有 {newItems.length - 5} 个机会</Typography.Text> : null}
        </div>
      ),
      okText: "查看借币监控",
      cancelText: "关闭",
      centered: true,
      onOk: focusBorrowMonitor,
      afterClose: () => {
        dialogOpenRef.current = false;
      }
    });
  }, [actionableItems, location.pathname, modal, navigate, signals.data?.status, signals.data?.updatedAt]);

  return contextHolder;
}

function Shell() {
  const navigate = useNavigate();
  const location = useLocation();
  const [navCollapsed, setNavCollapsed] = useState(true);

  useEffect(() => {
    const mobileNav = window.matchMedia("(max-width: 860px)");
    const keepMobileNavExpanded = () => {
      if (mobileNav.matches) setNavCollapsed(false);
    };
    keepMobileNavExpanded();
    mobileNav.addEventListener("change", keepMobileNavExpanded);
    return () => mobileNav.removeEventListener("change", keepMobileNavExpanded);
  }, []);

  const inDexHistory = location.pathname.startsWith("/dex-history");
  const selectedKey = location.pathname === "/exchange-announcements" || location.pathname.startsWith("/exchange-announcements/")
      ? "/exchange-announcements"
    : location.pathname.startsWith("/astro") || location.pathname.startsWith("/fs/astro-rules")
      ? "/astro"
    : inDexHistory
        ? "/dex-history"
        : location.pathname.startsWith("/fs")
          ? "/fs"
          : "/fs";
  const onSelect: MenuProps["onSelect"] = ({ key }) => navigate(key);

  return (
    <>
      <DexMappingAlertMonitor />
      <BorrowOpportunityAlertMonitor />
      <Layout className="app-shell">
      <Sider
        width={224}
        collapsedWidth={72}
        collapsible
        collapsed={navCollapsed}
        onCollapse={setNavCollapsed}
        className="app-sider"
      >
        <div className="brand" title="套利系统">
          <HomeOutlined />
          <Typography.Text strong>套利系统</Typography.Text>
        </div>
        <Menu mode="inline" selectedKeys={[selectedKey]} items={navItems} onSelect={onSelect} />
      </Sider>
      <Content className="app-content">
        <Suspense fallback={<PageFallback />}>
          <Routes>
            <Route path="/" element={<Navigate to="/fs" replace />} />
            <Route path="/exchange-announcements" element={<LocalExchangeNewsPage />} />
            <Route path="/fs" element={<FsPage />} />
            <Route path="/astro" element={<AstroAutoCardLayout />}>
              <Route index element={<Navigate to="status" replace />} />
              <Route path="status" element={<AstroStatusPage />} />
              <Route path="rules" element={<AstroScanRulesPage />} />
              <Route path="dex" element={<AstroDexMappingsPage />} />
            </Route>
            <Route path="/fs/astro-rules" element={<LegacyAstroRulesRedirect />} />
            <Route path="/dex-history" element={<DexHistoryPage />} />
            <Route path="*" element={<Navigate to="/fs" replace />} />
          </Routes>
        </Suspense>
      </Content>
      </Layout>
    </>
  );
}

export default function App() {
  return <RouterProvider router={router} />;
}

const router = createBrowserRouter([{ path: "*", element: <Shell /> }]);
