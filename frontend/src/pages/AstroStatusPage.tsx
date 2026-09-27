import { useState } from "react";
import { ExportOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Input, Modal, Space, Table, Tabs, Tag, Tooltip, Typography, message } from "antd";
import { Link } from "react-router-dom";
import { cryptoApi as api } from "../api/crypto";

const formatMetric = (value: number | null | undefined, digits = 2) =>
  value == null || !Number.isFinite(value) ? "—" : value.toFixed(digits);
const formatDuration = (value: number | null | undefined) =>
  value == null ? "—" : `${formatMetric(value / 1000)} 秒`;
const formatTime = (value: string | null | undefined) => value
  ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";

type RouteStatus = {
  symbol: string; type: string; buyExchange: string; sellExchange: string;
  mode: string; category?: string | null; error?: string | null;
  lastReason?: string; evidenceFresh: boolean;
};
const routeName = (row: RouteStatus) => `${row.symbol} ${row.type} · ${row.buyExchange} → ${row.sellExchange}`;
const routeReason = (row: RouteStatus, deadline: number) => {
  const reasons: Record<string, string> = {
    stale_direct_quote: "盘口报价过期",
    direct_quote_time_skew: "双腿盘口时间不同步",
    direct_spread_unavailable: "缺少可用价差",
    local_depth_deadline_exceeded: `本机盘口复核超过 ${deadline} 秒`,
    local_queue_timeout: "本机复核排队超时",
    cloud_depth_busy: "云端复核繁忙",
    cloud_queue_expired: "云端复核排队超时",
    cloud_depth_disabled: "云端复核未启用",
    cloud_depth_unsupported: "云端不支持该路线",
    cex_executable_depth_unavailable: "交易所盘口读取失败",
    direct_quote_unavailable: "未取得有效报价",
    direct_funding_unavailable: "精确资金费读取失败",
    okxdex_executable_quote_unavailable: "DEX 实际询价失败",
    okxdex_quote_not_advanced: "链上报价重复，未取得新报价",
    okxdex_quote_stale_at_submit: "链上报价已过期",
    below_threshold_or_rule_failed: "报价有效，未通过价差或其他规则",
    open_spread_non_positive: "报价有效，开仓价差不为正",
  };
  const reason = reasons[row.lastReason ?? ""];
  if (reason) return reason;
  const error = row.error ?? "";
  if (/429|rate limit/i.test(error)) return "行情接口限流";
  if (/盘口为空|深度为空/.test(error)) return "返回的盘口为空";
  if (/盘口时间无效或缺失/.test(error)) return "盘口缺少有效时间";
  if (/timeout|timed out|超时/i.test(error)) return "行情请求超时";
  return ({ capacity: "复核繁忙", quote_quality: "盘口质量未通过", transport: "行情通道异常" } as Record<string, string>)[row.category ?? ""]
    ?? (row.mode === "paused" || row.mode === "awaiting_recheck" ? "复核未通过，原因待确认" : "已取得有效报价");
};
const routeAction = (row: RouteStatus, cloudReady: boolean) => {
  if (!row.evidenceFresh) return "等待下一次实际复核更新，不计入当前故障";
  if (row.mode === "tencent_cloud") return "使用腾讯云复核；本机恢复后切回";
  if (row.mode !== "paused") return "按现有规则继续判断";
  const dex = [row.buyExchange, row.sellExchange].some(ex => ["okxdex", "pancakeswapv3"].includes(ex.toLowerCase()));
  if (row.category === "quote_quality") return dex ? "等待下一轮 DEX 真实询价，不因报价质量切云" : "等待新盘口，不因报价质量切云";
  if (dex) return "按扫描节奏重试 DEX 真实询价";
  if (row.lastReason === "direct_funding_unavailable") return "按资金费重试间隔重新读取";
  if (row.category === "capacity") return "等待复核名额或限流解除后重试";
  return cloudReady ? "等待重试；云端备用就绪，尚无有效复核" : "等待重试；云端备用暂不可用";
};

export default function AstroStatusPage() {
  const queryClient = useQueryClient();
  const [routeTab, setRouteTab] = useState("hot");
  const [reviewTarget, setReviewTarget] = useState<{submissionId: string; route: string} | null>(null);
  const [reviewEvidence, setReviewEvidence] = useState("");
  const [reviewedRoute, setReviewedRoute] = useState("");
  const [confirmNotExecuted, setConfirmNotExecuted] = useState(false);
  const resetReview = () => { setReviewTarget(null); setReviewEvidence(""); setReviewedRoute(""); setConfirmNotExecuted(false); };
  const statusQuery = useQuery({
    queryKey: ["astro-auto-card-status"], queryFn: () => api.astroAutoCardStatus(),
    refetchInterval: 2000, retry: false
  });
  const status = statusQuery.data;
  const reviewCurrent = status?.pendingSubmissions?.items.some(item => item.submissionId === reviewTarget?.submissionId && item.state === "needs_review") === true;
  const reviewAllowed = reviewCurrent && !statusQuery.isError && confirmNotExecuted && reviewEvidence.trim().length >= 10
    && reviewEvidence.trim().length <= 1000 && reviewedRoute.trim().toLowerCase() === reviewTarget?.route.toLowerCase();
  const scanner = status?.spreadScanner;
  const news = scanner?.newsPolicy;
  const degraded = scanner?.apiDegradedMode;
  const control = degraded?.routeControl;
  const cloudReady = degraded?.cloudBackup?.state === "ready";
  const deadline = degraded?.localDeadlineSeconds ?? 3;
  const activeRoutes = control?.routes.filter(row => row.evidenceFresh && ["paused", "tencent_cloud"].includes(row.mode)) ?? [];
  const activeCount = (control?.affectedRouteCount ?? 0) + (control?.cloudRouteCount ?? 0);
  const astroAdmin = status?.baseUrl && status.adminPrefix
    ? `${status.baseUrl.replace(/\/$/, "")}/${status.adminPrefix.replace(/^\//, "")}/dashboard`
    : undefined;
  const recheckSubmission = useMutation({
    mutationFn: api.recheckAstroSubmission,
    onSuccess: data => {
      message.success(data.result.message);
      queryClient.invalidateQueries({queryKey:["astro-auto-card-status"]});
    },
    onError: error => message.error(String(error)),
  });
  const resolveSubmission = useMutation({
    mutationFn: api.resolveAstroSubmissionNotExecuted,
    onSuccess: () => {
      message.success("已核实本次没有创建卡片，防重复锁已解除");
      resetReview();
      queryClient.invalidateQueries({queryKey:["astro-auto-card-status"]});
    },
    onError: error => message.error(String(error)),
  });
  return (
    <div className="astro-rules-page astro-status-page">
      <header className="astro-page-toolbar">
        <div><Typography.Title level={3}>运行状态</Typography.Title><Typography.Text type="secondary">{statusQuery.isError ? "刷新失败 · 以下为上次数据" : statusQuery.dataUpdatedAt ? `更新于 ${formatTime(new Date(statusQuery.dataUpdatedAt).toISOString())}` : "正在读取状态"}</Typography.Text></div>
        <Tooltip title="刷新状态"><Button aria-label="刷新状态" icon={<ReloadOutlined />} onClick={() => statusQuery.refetch()} /></Tooltip>
      </header>
      <section className={`astro-status-summary ${statusQuery.isError ? "is-stale" : ""}`} aria-label="运行概览">
        <div><span>行情扫描</span><strong>{!scanner ? "读取中" : statusQuery.isError ? "上次：扫描状态" : scanner.running ? "运行中" : scanner.enabled ? "等待运行" : "已关闭"}</strong><small>最近扫描 {formatTime(scanner?.lastScanAt)}</small></div>
        <div><span>自动建卡</span><strong>{!status ? "读取中" : statusQuery.isError ? "上次：建卡状态" : !status.enabled ? "已关闭" : !status.configured ? "待配置" : status.dryRun ? "试运行" : "已启用"}</strong><small>{statusQuery.isError ? "等待状态恢复" : status?.message || "等待读取建卡状态"}</small></div>
        <div><span>新卡状态</span><strong>{!status ? "—" : status.defaultPaused ? "默认暂停" : "默认运行"}</strong><small>单笔 {formatMetric(status?.defaultMinNotionalUsdt, 0)}–{formatMetric(status?.defaultMaxNotionalUsdt, 0)} USDT</small></div>
        <div><span>本轮候选 / 已确认</span><strong>{scanner?.candidateCount ?? "—"} / {scanner?.confirmedCount ?? "—"}</strong><small>确认候选不等于已建卡或已成交</small></div>
      </section>

      {status && !statusQuery.isError && !scanner?.lastError && scanner?.settingsHealth?.newCardsAllowed !== false && !status.pendingSubmissionCount && !activeCount && !status.automaticCleanup?.recentProtectedCards?.length
        ? <div className="astro-quiet-status">当前无待核对提交、复核受阻或清理保护记录</div> : null}
      {news && (news.lastError || news.storageError || news.cardCheckError || news.listingScheduleError || news.cardChecks.some(row => row.status === "pending"))
        ? <Alert type="warning" showIcon message="公告联动有待处理事项" description={<a href="#astro-news-policy">查看公告联动</a>} /> : null}
      {scanner?.settingsHealth?.newCardsAllowed === false ? (
        <Alert type="error" showIcon message="规则文件读取失败，已暂停新建卡" description="已有卡片不受影响。请检查或恢复已保存的规则文件，恢复后系统会重新核对规则。" />
      ) : null}
      {statusQuery.isError || scanner?.lastError ? (
        <Alert type="error" showIcon message="行情扫描状态异常" description={scanner?.lastError ?? String(statusQuery.error)} />
      ) : null}

      {(statusQuery.data?.pendingSubmissionCount ?? 0) > 0 ? <Alert type="warning" showIcon
        message={`${status?.pendingSubmissions?.waitingCount ?? status?.pendingSubmissionCount ?? 0} 条正在核对 · ${status?.pendingSubmissions?.reviewCount ?? 0} 条需人工核对`}
        description={<Space direction="vertical" size={4}>
          <span><strong>在哪里核对：</strong>打开 Astro 卡片列表，按币种搜索，再核对卡片类型和左右交易所。下方的本地提交编号只用于系统审计，不能在 Astro 中搜索。</span>
          {statusQuery.data?.pendingSubmissions?.items.map(item => <div key={`${item.name}:${item.type}:${item.buyEx}:${item.sellEx}`} style={{ borderTop: "1px solid #ead9a2", paddingTop: 10, width: "100%" }}>
            <strong>{item.name} {item.type} {item.buyEx}/{item.sellEx}</strong> · <Tag color={item.state === "needs_review" ? "orange" : "blue"}>{item.state === "needs_review" ? "提交结果未知，需人工核对" : "自动核对中"}</Tag>
            <div>提交 {formatTime(item.submittedAt)} · 已核对 {item.checkCount ?? 0} 次 · 最近核对 {formatTime(item.lastCheckedAt)}</div>
            {item.state === "needs_review" ? <>
              <div>请在 Astro 搜索 <strong>{item.name}</strong>，核对 <strong>{item.type}</strong> 和 <strong>{item.buyEx}/{item.sellEx}</strong>；提交时间用于判断是不是这次创建。</div>
              <Space wrap style={{ marginTop: 8 }}>
                {astroAdmin ? <Button href={astroAdmin} target="_blank" icon={<ExportOutlined />}>打开 Astro 卡片列表</Button> : null}
                <Button loading={recheckSubmission.isPending} disabled={!item.submissionId}
                  onClick={() => item.submissionId && recheckSubmission.mutate(item.submissionId)}>立即重新读取 Astro</Button>
                <Button danger disabled={!item.submissionId || resolveSubmission.isPending} onClick={() => {resetReview(); if (item.submissionId) setReviewTarget({submissionId:item.submissionId,route:`${item.name} ${item.type} ${item.buyEx}/${item.sellEx}`});}}>确认没有创建</Button>
              </Space>
            </> : <div>系统仍会按计划自动读取，无需手工处理。</div>}
            <details><summary style={{ cursor: "pointer" }}>提交详情</summary>
              <div>本地审计编号：{item.submissionId ?? "待补齐"}（仅供本系统追踪）</div>
              <div>提交异常：{item.error || "没有明确的成功结果"}</div>
              <div>最近读取：{item.lastCheckOutcome === "read_failed" ? `读取失败（${item.lastCheckError ?? "未知错误"}）` : item.lastCheckOutcome === "not_uniquely_found" ? "列表读取成功，未找到唯一匹配卡片" : "尚无核对记录"}</div>
            </details>
          </div>)}
        </Space>} /> : null}

      <Modal title="确认本次没有创建卡片" open={!!reviewTarget} okText="确认未执行，解除防重复锁" okButtonProps={{ danger:true, disabled:!reviewAllowed }}
        confirmLoading={resolveSubmission.isPending} onCancel={resetReview}
        onOk={() => reviewTarget && reviewAllowed && resolveSubmission.mutate({submissionId:reviewTarget.submissionId,evidence:reviewEvidence.trim(),reviewedRoute:reviewedRoute.trim(),confirmNotExecuted})}>
        <Typography.Paragraph><strong>{reviewTarget?.route}</strong></Typography.Paragraph>
        <Alert type="warning" showIcon message="列表里没找到卡片，不等于本次请求未执行。未能确认时请保留防重复锁。" />
        <Typography.Paragraph style={{ marginTop: 12, marginBottom: 6 }}>核对路线</Typography.Paragraph>
        <Input aria-label="核对路线" value={reviewedRoute} onChange={event => setReviewedRoute(event.target.value)} placeholder={reviewTarget?.route} />
        <Typography.Paragraph style={{ marginTop: 12, marginBottom: 6 }}>未执行依据（核对时间、记录来源及结果）</Typography.Paragraph>
        <Input.TextArea aria-label="未执行依据" rows={4} maxLength={1000} value={reviewEvidence} onChange={event => setReviewEvidence(event.target.value)} placeholder="10至1000字" />
        <Checkbox checked={confirmNotExecuted} onChange={event => setConfirmNotExecuted(event.target.checked)}>已核实此路线、此提交未执行，不仅是列表未显示</Checkbox>
        {!reviewCurrent ? <Alert type="warning" message="提交状态已变化，请关闭后刷新核对" /> : null}
      </Modal>

      {(status?.automaticCleanup?.recentProtectedCards?.length ?? 0) > 0 ? <section aria-label="自动清理保护">
        <Typography.Title level={4}>自动清理保护 · 需人工核对</Typography.Title>
        <Typography.Paragraph type="secondary">以下为最近10分钟实际遇到的配置回读不完整记录，卡片未被自动删除；不代表历史登记卡片总数。</Typography.Paragraph>
        {status?.automaticCleanup?.recentProtectedCards?.map(item => <div key={item.cardId} style={{ overflowWrap: "anywhere" }}>
          <Tag color="orange">无法自动清理</Tag>{item.name} {item.type} · {item.buyEx}/{item.sellEx}
          <div>暂不自动删除：无法读取{item.reason.split(":").slice(1).join(":").split(",").map(field => field === "priceAlertOnlyRise" ? "“价格报警仅上涨”设置" : field).join("、")} · 最近核对 {formatTime(item.observedAt)} · 卡片 {item.cardId}</div>
        </div>)}
      </section> : null}




      {activeCount > 0 ? (
        <Alert
          type={control?.affectedRouteCount ? "warning" : "info"}
          showIcon
          message={`${statusQuery.isError ? "上次读取：" : ""}${control?.affectedRouteCount ?? 0} 条等待有效复核 · ${control?.cloudRouteCount ?? 0} 条使用腾讯云 · 其他路线继续`}
          description={(
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {activeRoutes.slice(0, 4).map(row => <div key={row.route}>
                <strong>{routeName(row)}</strong>
                <div>原因：{routeReason(row, deadline)}；下一步：{routeAction(row, cloudReady)}。</div>
              </div>)}
              {activeCount > Math.min(activeRoutes.length, 4) ? <a href="#astro-route-status">另有 {activeCount - Math.min(activeRoutes.length, 4)} 条，查看下方复核明细（未返回的明细等待更新）</a> : null}
              <Typography.Text type="secondary">未取得有效复核的路线，本轮不建卡。</Typography.Text>
            </Space>
          )}
        />
      ) : null}

      <section className="astro-rules-card">
        <div className="astro-rules-section-head"><strong>扫描与复核</strong><Tag>目标 / 实际</Tag></div>
        <div className="astro-metric-strip">
          <div><span>全量发现间隔</span><strong>{scanner ? `${scanner.intervalSeconds} 秒` : "—"}</strong></div>
          <div><span>实际扫描耗时</span><strong>{formatDuration(scanner?.lastScanDurationMs)}</strong></div>
          <div><span>热点目标间隔</span><strong>{formatDuration(scanner?.hotMonitor?.intervalMs)}</strong></div>
          <div><span>正在复核</span><strong>{scanner?.hotMonitor?.inFlightRouteCount ?? "—"} / {scanner?.hotMonitor?.workers ?? "—"}</strong></div>
          <div><span>当前热点</span><strong>{scanner?.hotMonitor?.routeCount ?? "—"}</strong></div>
          <div><span>最长未重新开始</span><strong>{formatDuration(scanner?.hotMonitor?.maxRouteWaitSinceLastStartMs)}</strong></div>
        </div>
      </section>
      <section className="astro-rules-card" id="astro-route-status">
        <div className="astro-rules-section-head"><strong>路线明细</strong><span className="astro-cloud-queue">云端执行 {control?.cloudQueue.active ?? "—"}/{control?.cloudQueue.maxActive ?? "—"} · 排队 {control?.cloudQueue.waiting ?? "—"}/{control?.cloudQueue.maxWaiting ?? "—"}</span></div>
        <Tabs activeKey={routeTab} onChange={setRouteTab} items={[
          {key:"hot",label:`当前热点 · ${scanner?.hotMonitor?.routeCount ?? "—"}`},
          {key:"affected",label:`当前受影响 · ${activeCount}`},
          {key:"history",label:`历史记录 · ${control?.routes.filter(row => !row.evidenceFresh).length ?? 0}`}
        ]} />
        {routeTab === "hot" ? (<Table size="small" locale={{ emptyText: statusQuery.isLoading ? "正在读取热点" : statusQuery.isError ? "未取得最新热点数据" : "当前没有热点路线" }} rowKey={row => `${row.symbol}:${row.type}:${row.buyExchange}:${row.sellExchange}`}
          pagination={{ pageSize: 5, hideOnSinglePage: true }} scroll={{ x: 790 }} dataSource={scanner?.hotMonitor?.routeWaits ?? []}
          columns={[
            { title: "路线", render: (_, row) => <div className="astro-route-name"><strong>{row.symbol} <small>{row.type}</small></strong><span>{row.buyExchange} → {row.sellExchange}</span></div> },
            { title: "状态", render: (_, row) => row.listingProbe ? (row.inFlight ? "上市探测中" : "等待上市探测") : row.inFlight ? "复核中" : row.fundingWaiting ? "等待资金费更新" : "等待下一次复核" },
            { title: "首次开始前等待", render: (_, row) => row.firstDirectCheckStartedAtMs ? formatDuration(row.firstDirectCheckStartedAtMs - row.registeredAtMs) : "尚未开始" },
            { title: "距实际开始", render: (_, row) => formatDuration(row.waitSinceLastStartMs) },
            { title: "本轮目标间隔", render: (_, row) => formatDuration(row.pollIntervalMs) },
            { title: "上次检查耗时", render: (_, row) => formatDuration(row.lastDirectDurationMs) },
            { title: "已完成检查", dataIndex: "checks" }
          ]} />) : (          <Table size="small" rowKey="route" pagination={{ pageSize: 6 }} scroll={{ x: 760 }} locale={{ emptyText: routeTab === "affected" ? "当前没有受影响路线" : "暂无历史复核记录" }}
            dataSource={routeTab === "affected" ? activeRoutes : control?.routes.filter(row => !row.evidenceFresh) ?? []}
            expandable={{ expandedRowRender: row => <div className="astro-route-detail"><p>{routeAction(row, cloudReady)}</p><p>异常持续：{row.evidenceFresh ? `${row.durationSeconds ?? 0} 秒` : "无近期证据，不续计"}</p>{row.error ? <pre>{row.error}</pre> : null}</div> }}
            columns={[
              { title: "路线", key: "route", width: 200, render: (_, row) => routeName(row) },
              { title: "当前状态", key: "mode", width: 130, render: (_, row) => <Tag color={!row.evidenceFresh ? "default" : row.mode === "paused" ? "orange" : row.mode === "tencent_cloud" ? "blue" : "green"}>{!row.evidenceFresh ? "历史记录待更新" : row.mode === "paused" ? "本轮暂不建卡" : row.mode === "tencent_cloud" ? "腾讯云复核" : "本机复核"}</Tag> },
              { title: "原因", key: "reason", width: 220, render: (_, row) => <span>{!row.evidenceFresh ? "上次：" : ""}{routeReason(row, deadline)}</span> },
              { title: "最近检查 / 成功（北京时间）", key: "time", width: 195, render: (_, row) => <div><div>检查 {formatTime(row.lastCheckedAt)}</div><Typography.Text type="secondary">成功 {formatTime(row.lastSuccessfulVerificationAt)}</Typography.Text></div> },
            ]} />)}
      </section>
      <details className="astro-rules-card"><summary>复核机制与固定要求</summary>          <details style={{ marginBottom: 16 }}>
            <summary style={{ cursor: "pointer" }}>查看复核与提醒机制</summary>
            <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>CEX 本机复核超过 {deadline} 秒可按路线切云；DEX 按原扫描节奏重新询价。盘口过期、时间不同步只等待新行情，不推送、不触发应急切换。本机故障推送{degraded?.localFaultPushEnabled ? "已启用" : "已暂停"}；符合条件的连接或排队故障持续 {control?.faultDelaySeconds ?? 20} 秒才汇总提醒，全局间隔 {(control?.globalPushIntervalSeconds ?? 600) / 60} 分钟。CEX 恢复探针每 {degraded?.recoveryProbe.intervalSeconds ?? 2} 秒运行，本机真实盘口稳定 {control?.recoveryStableSeconds ?? 10} 秒后切回。DEX 使用下一次真实路线复核更新状态。无近期证据不当作当前故障；无有效复核不建卡。</Typography.Paragraph>
          </details>
        <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
          热点目标间隔不是每条路线的速度保证。空闲名额立即补位；慢请求、路线数量和限流仍会影响等待。
          交易所–交易所建卡需要两次独立的双腿盘口验证。链上–交易所建卡前连续核实 3 次真实询价，每轮间隔 1 秒，每轮核对同数量合约深度；重复时间戳不计数，任一轮不达标就停止本轮建卡。CEX 最终报价年龄不超过 {scanner?.finalRevalidation?.maxQuoteAgeSeconds ?? 3} 秒，
          两腿时间差不超过 {scanner?.finalRevalidation?.maxQuoteSkewSeconds ?? 1.25} 秒；提交前过期则等待新复核。
          {statusQuery.data?.sdkReadTotalSeconds != null ? ` 卡片列表读取总预算 ${statusQuery.data.sdkReadTotalSeconds} 秒，提交后的回读确认总预算 ${statusQuery.data.verificationTimeoutSeconds} 秒。` : ""}
          {scanner?.hotMonitor?.listingProbeIntervalSeconds != null ? ` 已到正式上线时间但 Pulse 未覆盖的路线，每次探测至少间隔 ${scanner.hotMonitor.listingProbeIntervalSeconds} 秒、最多占 ${scanner.hotMonitor.listingProbeMaxConcurrent ?? 1} 个名额，繁忙时普通热点优先；拿到真实双腿盘口后再升级。` : ""}
        </Typography.Paragraph>

        <Typography.Paragraph type="secondary">Pulse 资金费初筛不新增 API 请求；盘口条件通过后查精确资金费，复用 10 秒缓存。Pulse 明显负费率最多延后 10 秒，临近零、缺失或过期时继续复核；差价达到 2.3% 直接复核盘口，实际差价 ≥ 2.5% 才豁免。API 失败按 2／5／10 秒重试，FF 不检查资金费。</Typography.Paragraph>
</details>
      {(status?.pendingSubmissions?.recentResolutions?.length ?? 0) > 0 ? <details className="astro-rules-card" open>
        <summary style={{ cursor: "pointer" }}>最近建卡结果</summary>
        {status?.pendingSubmissions?.recentResolutions?.map(item => <div key={item.submissionId ?? `${item.resolvedAt}:${item.name}`} style={{ marginTop: 8 }}>
          <Tag color={item.state === "confirmed" ? "green" : "default"}>{item.state === "confirmed" ? "已确认成功" : "未执行，提交已结束"}</Tag>
          {item.name} {item.type} {item.buyEx}/{item.sellEx} · {formatTime(item.resolvedAt)} · {item.resolution}
        </div>)}
      </details> : null}
      {news ? <details className="astro-rules-card" id="astro-news-policy">
        <summary style={{ cursor: "pointer" }}>公告联动 · {news.blockCount} 项下架限制 · {news.listingSymbols.length} 个公告预建币种
          {news.lastError || news.storageError || news.cardCheckError || news.listingScheduleError || news.cardChecks.some(row => row.status === "pending") ? " · 待处理" : ""}
        </summary>
        <p>每 {news.intervalSeconds} 秒集中读取新闻；下架按币种、交易所和现货／合约限制新开仓，已有卡片保留平仓设置。上架公告可提前创建暂停卡：双方已公告，或一方已交易＋另一方已公告待上市。不要求开市、价差、资金费或盘口达标。</p>
        <p>新闻源更新：{formatTime(news.sourceUpdatedAt)} · 卡片核对：{formatTime(news.lastCardCheckAt)}</p>
        {news.lastError || news.storageError || news.cardCheckError ? <Alert type="warning" showIcon message="公告联动部分未完成" description={`新闻：${news.lastError || "正常"}；持久化：${news.storageError || "正常"}；卡片核对：${news.cardCheckError || "正常"}。已知下架限制继续保留。`} /> : null}
        {news.listingSymbols.length ? <p>公告预建：{news.listingSymbols.join("、")}</p> : null}
        <p>预建卡采用已保存的开／平仓阈值作为初始设置，不代表当前可成交差价；不会自动启动，也不会因尚无行情或差价不达标而自动清理。</p>
        {news.listingScheduleError ? <Alert type="warning" message={`公告预建暂未完成：${news.listingScheduleError}`} /> : null}
        {news.listingRoutes?.map(row => <div key={`${row.symbol}:${row.type}:${row.buyExchange}:${row.sellExchange}`}>
          <Tag color="blue">{row.category === "both_announced" ? "双方已公告" : "已交易＋已公告"}</Tag>
          {row.symbol} {row.type} · {row.buyExchange}/{row.sellExchange} · {({ existing: "卡片已存在", syncing: "提交处理中", queued: "排队中", submission_pending: "提交待确认", waiting_submission: "等待提交" } as Record<string, string>)[row.state] || row.state}
        </div>)}
        {news.unresolvedNoticeCount ? <p>另有 {news.unresolvedNoticeCount} 条公告待解析，尚未确认具体限制范围。</p> : null}
        <Space wrap>{news.blocks.map(row => <a key={`${row.symbol}:${row.exchange}:${row.market}`} href={row.sourceUrl} target="_blank" rel="noreferrer"><Tag color="red">{row.symbol} · {row.exchange} · {row.market === "spot" ? "现货" : "合约"} 禁开</Tag></a>)}</Space>
        {news.cardChecks.map(row => <div key={row.id}>{row.symbol} {row.type} · {row.buyExchange}/{row.sellExchange}：{row.status === "confirmed" ? "已复读确认禁止开仓" : row.status === "removed" ? "卡片已不存在" : row.status === "route_changed" ? "路线已改变，等待下轮核对" : `禁止开仓待确认（${row.error || "等待核对"}）`}</div>)}
      </details> : null}






      <section className="astro-rules-card">
        <div className="astro-rules-section-head"><strong>行情源与卡片保护</strong><Link to="/astro/rules">管理规则与行情源</Link></div>
        <Space size={[16, 8]} wrap>
          <span>已订阅行情源：{scanner?.subscriptions.length ?? "—"}</span>
          <span>Pulse 成功 / 失败：{scanner?.pulseSources?.successCount ?? "—"} / {scanner?.pulseSources?.failureCount ?? "—"}</span>
          <span>等待删除后重新触发：{scanner?.deleteRearm?.activeGuardCount ?? "—"}</span>
          <span>失效卡观察中：{status?.automaticCleanup?.invalidTrackingCount ?? "—"}</span>
        </Space>
        <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
          {status?.automaticCleanup?.enabled ? `自动清理先保留新卡 ${status.automaticCleanup.graceSeconds} 秒，并要求连续失效 ${status.automaticCleanup.continuousInvalidSeconds} 秒。删除前重新核对持仓、成交与配置；无法确认安全的卡片保留。` : "自动清理未开启。"}
        </Typography.Paragraph>
      </section>
    </div>
  );
}
