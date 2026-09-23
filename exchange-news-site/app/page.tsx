'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  CircleAlert,
  Clock3,
  ExternalLink,
  RefreshCw,
  ScrollText,
  ShieldCheck,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import type { ListingReminder, MonitorSnapshot } from '@/lib/exchange-news-types';

type ReminderGroup = {
  id: string;
  reminders: ListingReminder[];
  nextAt: number;
};

const UNMATCHED_PENDING_MAX_LEAD_MS = 30 * 24 * 60 * 60 * 1_000;

function shouldShowReminder(reminder: ListingReminder, now: number) {
  const scheduledAt = Date.parse(reminder.scheduledAt ?? '');
  if (!Number.isFinite(scheduledAt)) return isTodayBeijing(reminder.publishedAt ?? reminder.firstDetectedAt, now);
  if (scheduledAt <= now && !isTodayBeijing(reminder.scheduledAt!, now)) return false;
  const isUnconfirmedFarFuturePlaceholder =
    !reminder.announcementMatched &&
    !reminder.announcementUrl &&
    String(reminder.marketStatus ?? '').toUpperCase() === 'PENDING_TRADING' &&
    scheduledAt - now > UNMATCHED_PENDING_MAX_LEAD_MS;
  return !isUnconfirmedFarFuturePlaceholder;
}

function beijingTime(value?: string | null, empty = '—') {
  if (!value) return empty;
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit',
    minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
}

function statusTone(status: string) {
  if (status === 'ok' || status === 'running' || status === 'tradable') return 'good';
  if (status === 'verifying' || status === 'market_only' || status === 'starting') return 'watch';
  return 'bad';
}

function logLabel(level: 'info' | 'success' | 'warning' | 'error') {
  return level === 'success' ? '完成' : level === 'warning' ? '注意' : level === 'error' ? '异常' : '记录';
}

function normalizedAnnouncementTemplate(reminder: ListingReminder) {
  let title = (reminder.announcementTitle ?? '').toUpperCase();
  const variableParts = [reminder.marketSymbol, `${reminder.symbol}USDT`, reminder.symbol]
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);
  for (const part of variableParts) title = title.replaceAll(part.toUpperCase(), '');
  return title.replace(/\s+/g, ' ').trim();
}

function isSameAnnouncementBatch(a: ListingReminder, b: ListingReminder) {
  if ((a.action ?? 'listing') !== (b.action ?? 'listing')) return false;
  if (!a.announcementMatched || !b.announcementMatched || !a.announcementTitle || !b.announcementTitle) return false;
  if (a.announcementUrl && a.announcementUrl === b.announcementUrl) return true;
  if (a.exchange !== b.exchange || a.marketType !== b.marketType || a.assetType !== b.assetType) return false;
  if (normalizedAnnouncementTemplate(a) !== normalizedAnnouncementTemplate(b)) return false;
  const aPublishedAt = Date.parse(a.publishedAt ?? '');
  const bPublishedAt = Date.parse(b.publishedAt ?? '');
  return Number.isFinite(aPublishedAt) && Number.isFinite(bPublishedAt) && Math.abs(aPublishedAt - bPublishedAt) <= 60_000;
}

function isSameUnmatchedCategoryBatch(a: ListingReminder, b: ListingReminder) {
  if ((a.action ?? 'listing') !== (b.action ?? 'listing')) return false;
  if (a.announcementMatched || b.announcementMatched || a.announcementTitle || b.announcementTitle) return false;
  if (a.exchange !== b.exchange || a.marketType !== b.marketType || a.assetType !== b.assetType) return false;
  if (a.contractKind !== b.contractKind) return false;
  const aScheduledAt = Date.parse(a.scheduledAt ?? '');
  const bScheduledAt = Date.parse(b.scheduledAt ?? '');
  return Number.isFinite(aScheduledAt) && Number.isFinite(bScheduledAt) && Math.abs(aScheduledAt - bScheduledAt) <= 60_000;
}

function reminderOrderAt(reminder: ListingReminder) { return Date.parse(reminder.scheduledAt ?? reminder.publishedAt ?? reminder.firstDetectedAt); }

function reminderReadKey(item: ListingReminder) {
  return item.announcementUrl && item.announcementTitle ? item.announcementUrl
    : `reminder:${encodeURIComponent(JSON.stringify([item.id, item.action, item.scheduledAt]))}`;
}

function sortReminderGroups(groups: ReminderGroup[], readMarks: Record<string, boolean>): ReminderGroup[] {
  const unread = (group: ReminderGroup) => group.reminders.some(item => !readMarks[reminderReadKey(item)]);
  const latest = (group: ReminderGroup) => Math.max(0, ...group.reminders.map(item => {
    const published = Date.parse(item.publishedAt ?? '');
    return Number.isFinite(published) ? published : Date.parse(item.firstDetectedAt) || 0;
  }));
  return [...groups].sort((a, b) => Number(unread(b)) - Number(unread(a)) || latest(b) - latest(a) || a.id.localeCompare(b.id));
}

function buildReminderGroups(reminders: ListingReminder[]): ReminderGroup[] {
  const parents = reminders.map((_, index) => index);
  const find = (index: number): number => {
    if (parents[index] !== index) parents[index] = find(parents[index]);
    return parents[index];
  };
  const union = (left: number, right: number) => {
    const leftRoot = find(left);
    const rightRoot = find(right);
    if (leftRoot !== rightRoot) parents[rightRoot] = leftRoot;
  };

  for (let left = 0; left < reminders.length; left += 1) {
    for (let right = left + 1; right < reminders.length; right += 1) {
      if ((reminders[left].action ?? 'listing') !== (reminders[right].action ?? 'listing')) continue;
      if (
        (reminders[left].symbol === reminders[right].symbol && reminders[left].contractKind === reminders[right].contractKind) ||
        isSameAnnouncementBatch(reminders[left], reminders[right]) ||
        isSameUnmatchedCategoryBatch(reminders[left], reminders[right])
      ) {
        union(left, right);
      }
    }
  }

  const grouped = new Map<number, ListingReminder[]>();
  reminders.forEach((reminder, index) => {
    const root = find(index);
    grouped.set(root, [...(grouped.get(root) ?? []), reminder]);
  });

  return [...grouped.values()]
    .map((items) => ({
      id: items.map((item) => item.id).sort().join('|'),
      reminders: items.sort((a, b) => reminderOrderAt(a) - reminderOrderAt(b)),
      nextAt: Math.min(...items.map(reminderOrderAt)),
    }))
    .sort((a, b) => a.nextAt - b.nextAt);
}

function compactTimeRange(values: Array<string | null>, empty = '来源未提供时间') {
  const times = [...new Set(values.filter((value): value is string => Boolean(value)))]
    .sort((a, b) => Date.parse(a) - Date.parse(b));
  if (!times.length) return empty;
  const first = beijingTime(times[0]);
  if (times.length === 1) return first;
  const last = beijingTime(times[times.length - 1]);
  const [firstDate] = first.split(' ');
  const [lastDate, lastClock] = last.split(' ');
  return firstDate === lastDate && lastClock ? `${first}–${lastClock}` : `${first}–${last}`;
}

function scheduledReminderGroups(reminders: ListingReminder[]) {
  const groups = new Map<string, ListingReminder[]>();
  for (const reminder of reminders) {
    const key = `${reminder.scheduledAt ?? 'unknown'}:${reminder.openingSuspendsAt ?? ''}`;
    groups.set(key, [...(groups.get(key) ?? []), reminder]);
  }
  return [...groups.values()];
}

function countdownText(scheduledAt: string, now: number) {
  const seconds = Math.floor((Date.parse(scheduledAt) - now) / 1000);
  if (seconds <= 0) return '计划时间已到';
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const rest = seconds % 60;
  const clock = [hours, minutes, rest].map((value) => String(value).padStart(2, '0')).join(':');
  return days > 0 ? `${days}天 ${clock}` : clock;
}

function isTodayBeijing(value: string, now: number) {
  const formatter = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });
  return formatter.format(new Date(value)) === formatter.format(new Date(now));
}

function symbolTarget(symbol: string) {
  return `https://funding-v2.astro-btc.xyz/?coin=${encodeURIComponent(symbol)}`;
}

function marketTypeLabel(type: ListingReminder['marketType']) {
  return type === 'spot' ? '现货' : '合约';
}

function chineseOnlyTitle(title: string | null, symbol: string) {
  if (!title) return null;
  if (/[\u3400-\u4dbf\u4e00-\u9fff]/.test(title)) return title;
  if (/delay|postpone|reschedule/i.test(title)) return `${symbol} 永续合约上线延期公告`;
  return `${symbol} 交易所公告`;
}

function AnnouncementRecords({ reminders, readMarks, markRead, enabled }: { reminders: ListingReminder[]; readMarks: Record<string, boolean>; markRead: (url: string | string[], read: boolean) => void; enabled: boolean }) {
  const announcements = reminders.filter((reminder) => reminder.announcementUrl && reminder.announcementTitle);
  const unmatched = reminders.filter((reminder) => !reminder.announcementUrl || !reminder.announcementTitle);
  const compactBatch = announcements.length > 1 && announcements.every((reminder) => isSameAnnouncementBatch(announcements[0], reminder));
  const unmatchedGroups = [...new Map(unmatched.map((reminder) => [
    `${reminder.exchange}:${reminder.marketType}:${reminder.assetType}`,
    unmatched.filter((item) => item.exchange === reminder.exchange && item.marketType === reminder.marketType && item.assetType === reminder.assetType),
  ])).values()];

  return (
    <div className="space-y-1.5">
      {compactBatch ? (
        <div className="flex flex-wrap items-center gap-x-1 text-base font-medium leading-6">
          <span className="text-sm text-muted-foreground">[{announcements[0].exchangeName}]</span>
          <span>{announcements[0].action === 'delisting' ? '下架' : '上线'}</span>
          {[...new Map(announcements.map((reminder) => [reminder.symbol, reminder])).values()].map((reminder, index, items) => (
            <span className="inline-flex items-center gap-1" key={`${reminder.id}-batch-news`}>
              <a className="text-blue-600 hover:text-blue-800 hover:underline" href={reminder.announcementUrl ?? undefined} onClick={() => markRead(reminder.announcementUrl!, true)} target="_blank" rel="noreferrer">{reminder.symbol}</a>
              {index < items.length - 1 && <span className="text-muted-foreground">/</span>}
            </span>
          ))}
          <span>{announcements.some((reminder) => reminder.assetType === 'stock') ? '股票' : ''}{announcements[0].marketType === 'spot' ? '现货' : '永续合约'}公告</span>
          <ExternalLink className="size-4 shrink-0 text-muted-foreground" />
        </div>
      ) : (
        [...new Map(announcements.map((reminder) => [reminder.announcementUrl, reminder])).values()].map((reminder) => (
          <a className="flex items-start gap-1.5 text-base font-medium leading-6 hover:text-primary" key={`${reminder.id}-news`} href={reminder.announcementUrl ?? undefined} onClick={() => markRead(reminder.announcementUrl!, true)} target="_blank" rel="noreferrer">
            <span className="text-sm text-muted-foreground">[{reminder.exchangeName}]</span><span>{chineseOnlyTitle(reminder.announcementTitle, reminder.symbol)}</span><ExternalLink className="mt-1 size-4 shrink-0 text-muted-foreground" />
          </a>
        ))
      )}
      {enabled && <div className="flex flex-wrap gap-2">
        {[...new Map(announcements.map(row => [row.announcementUrl!, row])).values()].map(row => (
          <button key={row.announcementUrl} type="button" className={`rounded border px-2 text-sm ${readMarks[row.announcementUrl!] ? 'text-slate-500 bg-slate-100' : 'text-blue-700 border-blue-300'}`} onClick={() => markRead(row.announcementUrl!, !readMarks[row.announcementUrl!])} title="点击切换已读/未读，不影响消息推送">
            {readMarks[row.announcementUrl!] ? '✓ 已读' : '● 未读'}{announcements.some(other => other.announcementUrl !== row.announcementUrl) ? ` · ${row.symbol}` : ''}
          </button>
        ))}
      </div>}
      {unmatchedGroups.map((items) => {
        const symbols = [...new Set(items.map((item) => item.symbol))];
        const label = items[0].assetType === 'stock' ? '股票合约' : items[0].marketType === 'spot' ? '现货' : '合约';
        const keys = items.map(reminderReadKey);
        const read = keys.every(key => readMarks[key]);
        return <div className="flex flex-wrap items-center gap-2 text-base font-medium text-amber-700" key={`${items[0].exchange}:${items[0].marketType}:${items[0].assetType}-unmatched`}>
          <span>[{items[0].exchangeName}] {symbols.length > 1 ? `${symbols.length} 个${label}` : label}列表已出现，未匹配公告</span>
          {enabled && <button type="button" className={`rounded border px-2 text-sm ${read ? 'text-slate-500 bg-slate-100' : 'text-blue-700 border-blue-300'}`} onClick={() => markRead(keys, !read)} title="标记本组提醒；不影响推送，新增标的或计划时间变化后仍提示未读">{read ? '✓ 已读' : '● 未读'}</button>}
        </div>;
      })}
    </div>
  );
}

export default function Home({ localFeatures = false }: { localFeatures?: boolean } = {}) {
  const [readMarks, setReadMarks] = useState<Record<string, boolean>>({});
  const [readError, setReadError] = useState<string | null>(null);
  useEffect(() => {
    if (!localFeatures) return;
    void fetch('/api/read-marks').then(async response => {
      if (!response.ok) throw new Error('已读记录暂不可用');
      setReadMarks(await response.json());
    }).catch(() => setReadError('已读记录暂不可用，不能确认阅读状态。'));
  }, [localFeatures]);
  const markRead = (url: string | string[], read: boolean) => {
    if (!localFeatures) return;
    const keys = Array.isArray(url) ? url : [url];
    void fetch('/api/read-marks', { method: 'POST', headers: { 'content-type': 'application/json', 'x-news-local': '1' }, body: JSON.stringify({ keys, read }) }).then(async response => {
      if (!response.ok) throw new Error('保存失败');
      const saved = await response.json() as Record<string, boolean>;
      setReadMarks(previous => ({ ...previous, ...Object.fromEntries(keys.map(key => [key, Boolean(saved[key])])) })); setReadError(null);
    }).catch(() => setReadError('已读标记保存失败，请重试。'));
  };
  const [snapshot, setSnapshot] = useState<MonitorSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastLoadedAt, setLastLoadedAt] = useState<Date | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const loadInFlight = useRef(false);
  const [retryAt, setRetryAt] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (loadInFlight.current) return;
    loadInFlight.current = true;
    setLoading(true);
    try {
      const response = await fetch('/api/monitor', { cache: 'no-store', signal: AbortSignal.timeout(18_000) });
      const payload = (await response.json()) as MonitorSnapshot & { error?: string; retryAt?: string | null };
      setRetryAt(payload.retryAt ?? null);
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      if (!Array.isArray(payload.listingReminders) || !Array.isArray(payload.activityLogs)) throw new Error('数据未完整读取，请稍后重试。');
      setSnapshot(payload);
      setError(null);
      setLastLoadedAt(new Date());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      loadInFlight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => void load(), 15_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const reminderGroups = useMemo(
    () => sortReminderGroups(buildReminderGroups((snapshot?.listingReminders ?? []).filter((reminder) => shouldShowReminder(reminder, now))), readMarks),
    [snapshot?.listingReminders, now, readMarks],
  );

  return (
    <main className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-[1480px] px-4 py-5 sm:px-6 lg:py-6">
        <header className="monitor-header">
          <div className="flex flex-wrap items-center gap-2.5">
            <ShieldCheck className="size-6 text-primary" />
            <h1 className="text-3xl font-semibold tracking-[-0.035em]">交易所新闻监控</h1>
            <Badge className={`status-pill status-${error ? 'bad' : statusTone(snapshot?.status ?? 'starting')}`}>
              {error ? '监控不可用' : snapshot?.status === 'running' ? '运行中' : snapshot?.status === 'degraded' ? '部分降级' : snapshot?.status === 'stopped' ? '已停止' : '读取中'}
            </Badge>
          </div>
          <div className="flex items-center gap-2">
            <div className="freshness-chip"><Activity className="size-3.5" />15 秒刷新 · {lastLoadedAt ? `上次成功 ${beijingTime(lastLoadedAt.toISOString())}` : error ? '尚未读到数据' : '等待数据'}</div>
            <Button className="h-9 px-4 text-base" variant="outline" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={loading ? 'animate-spin' : ''} data-icon="inline-start" />立即刷新
            </Button>
          </div>
        </header>

        {error && (
          <div className="mb-4 flex items-center gap-2 rounded-xl border border-red-300 bg-red-50 px-4 py-3 text-base text-red-800">
            <CircleAlert className="size-4 shrink-0" /><div role="alert">{error}{retryAt && <span> 重试时间：{beijingTime(retryAt)}（北京时间）。</span>}{snapshot && <div className="mt-1">下方保留上次成功读取的数据，不代表当前监控状态。</div>}</div>
          </div>
        )}

        {readError && <p role="alert" className="mb-3 text-amber-800">{readError}</p>}
        <section className="panel overflow-hidden">
          <div className="panel-heading">
            <div><h2><Clock3 className="size-4 text-primary" />今日上下架提醒 / 待发生</h2><p>北京时间今天及未来计划；上架、下架分开，同批公告合并。到达计划时间不代表市场状态已确认。</p></div>
            <Badge variant="outline">{snapshot ? `${error ? '上次数据 · ' : ''}${reminderGroups.length} 组提醒` : error ? '数量未知' : '读取中'}</Badge>
          </div>
          <div className="overflow-x-auto">
            <Table className="[&_td]:py-3 [&_td]:text-base [&_th]:h-12 [&_th]:text-base">
              <TableHeader><TableRow className="bg-muted/55 hover:bg-muted/55"><TableHead className="min-w-[190px] pl-5">标的</TableHead><TableHead className="min-w-[120px]">交易所</TableHead><TableHead className="min-w-[190px]">新闻发布时间</TableHead><TableHead className="min-w-[230px]">计划时间 / 倒计时</TableHead><TableHead className="min-w-[420px]">公告记录</TableHead></TableRow></TableHeader>
              <TableBody>
                {reminderGroups.map((group) => {
                  const symbols = [...new Set(group.reminders.map((reminder) => reminder.symbol))];
                  const exchanges = [...new Map(group.reminders.map((reminder) => [reminder.exchange, reminder])).values()];
                  const marketTypes = [...new Set(group.reminders.map((reminder) => reminder.marketType))];
                  const scheduledGroups = scheduledReminderGroups(group.reminders);
                  const delisting = group.reminders[0].action === 'delisting';
                  return <TableRow key={group.id}>
                    <TableCell className="pl-5 align-top">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        {symbols.map((symbol, index) => <span className="inline-flex items-center gap-2" key={symbol}><a className="text-lg font-bold text-blue-600 hover:text-blue-800 hover:underline" href={symbolTarget(symbol)} target="_blank" rel="noreferrer">{symbol}</a>{index < symbols.length - 1 && <span className="text-muted-foreground">/</span>}</span>)}
                        {marketTypes.map((marketType) => (
                          <Badge
                            className={marketType === 'spot' ? 'border-emerald-400 bg-emerald-50 text-sm font-semibold text-emerald-800' : 'border-blue-400 bg-blue-50 text-sm font-semibold text-blue-800'}
                            key={marketType}
                          >
                            {marketTypeLabel(marketType)}
                          </Badge>
                        ))}
                        {group.reminders.some((reminder) => reminder.assetType === 'stock') && <Badge className="border-amber-400 bg-amber-50 text-sm font-semibold text-amber-800">股票</Badge>}
                        <Badge className={delisting ? 'border-red-400 bg-red-50 text-sm font-semibold text-red-700' : 'border-blue-300 bg-blue-50 text-sm text-blue-700'}>{delisting ? '下架' : '上架'}</Badge>
                        {group.reminders.some((reminder) => reminder.contractKind === 'delivery') && <Badge variant="outline" className="text-sm">交割 / 新增期限</Badge>}
                      </div>
                    </TableCell>
                    <TableCell className="align-top"><div className="flex flex-wrap gap-1.5">{exchanges.map((reminder) => <Badge className="text-sm" key={`${reminder.exchange}-market`} variant="outline">{reminder.exchangeName}</Badge>)}</div></TableCell>
                    <TableCell className="align-top font-mono text-sm">{compactTimeRange(group.reminders.map((reminder) => reminder.publishedAt), group.reminders.some((reminder) => reminder.announcementMatched) ? '来源未提供时间' : '未匹配公告')}</TableCell>
                    <TableCell className="align-top"><div className="space-y-1">{scheduledGroups.map((items) => {
                      const scheduledAt = items[0].scheduledAt;
                      const future = Date.parse(scheduledAt ?? '') > now;
                      const labels = [...new Set(items.map(item => exchanges.length > 1 ? `${item.symbol} · ${item.exchangeName}` : item.symbol))];
                      return <div key={`${scheduledAt}:${items[0].openingSuspendsAt ?? ''}`} className="flex flex-wrap items-center gap-2">
                        {scheduledGroups.length > 1 && <span className="text-sm font-semibold">{labels.join(' / ')}</span>}
                        <span className="font-mono text-sm font-semibold">{delisting ? '下架 ' : ''}{beijingTime(scheduledAt, '计划时间待确认')}</span>
                        <Badge className={`status-pill status-${delisting ? 'bad' : future ? 'watch' : 'good'} text-sm`}>{future && scheduledAt ? `${isTodayBeijing(scheduledAt, now) ? '' : '未发生 · '}${countdownText(scheduledAt, now)}` : delisting ? scheduledAt ? '下架计划时间已到' : '暂无倒计时' : items.every(item => item.tradableAt) ? '已确认可交易' : scheduledAt ? '计划时间已到 · 待盘口确认' : '暂无倒计时'}</Badge>
                        {delisting && items[0].openingSuspendsAt && <div className="w-full text-sm text-red-700">停止开新仓：{beijingTime(items[0].openingSuspendsAt)} · {countdownText(items[0].openingSuspendsAt, now)}</div>}
                      </div>;
                    })}</div>
                      <details className="mt-1 text-sm text-muted-foreground"><summary className="cursor-pointer">{delisting ? '首次发现时间' : '发现 / 盘口确认时间'}</summary>{group.reminders.map((reminder) => <div key={`${reminder.id}-times`} className="mt-1">{reminder.exchangeName} {reminder.symbol}：发现 {beijingTime(reminder.firstDetectedAt)}{!delisting && <>；{reminder.recoveredAfterGap ? '补查确认' : '首次盘口'} {beijingTime(reminder.tradableAt, '待确认')}</>}</div>)}</details>
                    </TableCell>
                    <TableCell className="whitespace-normal align-top"><AnnouncementRecords reminders={group.reminders} readMarks={readMarks} markRead={markRead} enabled={localFeatures} /></TableCell>
                  </TableRow>;
                })}
                {!reminderGroups.length && <TableRow><TableCell colSpan={5} className="h-28 text-center text-muted-foreground">{error ? '数据读取失败，暂时无法判断是否有上下架提醒。' : !snapshot ? '正在读取上下架提醒…' : '今天暂无上下架提醒，也没有未来待发生计划。'}</TableCell></TableRow>}
              </TableBody>
            </Table>
          </div>
        </section>

        <section className="panel mt-4 overflow-hidden">
          <div className="panel-heading">
            <div><h2><ScrollText className="size-4 text-primary" />运行日志</h2><p>只记录新增合约、盘口结果、公告匹配、推送及来源异常 / 恢复，不逐条记录轮询。</p></div>
            <Badge variant="outline">最近 50 条</Badge>
          </div>
          <div className="divide-y divide-border">
            {(snapshot?.activityLogs ?? []).map((log) => (
              <div className="grid gap-1 px-5 py-3 text-base sm:grid-cols-[175px_68px_1fr] sm:items-center" key={log.id}>
                <time className="font-mono text-sm text-muted-foreground">{beijingTime(log.at)}</time>
                <Badge className={`status-pill status-${log.level === 'success' ? 'good' : log.level === 'info' ? 'watch' : 'bad'} w-fit`}>{logLabel(log.level)}</Badge>
                <div className="min-w-0">
                  <span>{log.message}</span>
                  {(log.exchange || log.symbol) && <span className="ml-2 font-mono text-sm text-muted-foreground">{[log.exchange?.toUpperCase(), log.symbol].filter(Boolean).join(' · ')}</span>}
                </div>
              </div>
            ))}
            {!snapshot?.activityLogs?.length && <div className="px-5 py-8 text-center text-sm text-muted-foreground">{error ? '运行日志未读取，不能据此判断监控或推送是否正常。' : !snapshot ? '正在读取运行日志…' : '日志将在下一次重要状态变化时产生。'}</div>}
          </div>
        </section>

        <footer className="mt-5 flex flex-col gap-1 border-t border-border pt-4 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <span>仅作监控与记录；不代表已成交，不触发自动下单。</span><span>监控调度最后完成：{beijingTime(snapshot?.runtime.lastCycleAt)}</span>
        </footer>
      </div>
    </main>
  );
}
