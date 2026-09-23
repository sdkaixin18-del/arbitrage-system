export type SourceStatus = {
  exchange: string;
  kind: 'contracts' | 'news';
  status: string;
  count: number;
  lastCheckedAt: string;
  message: string | null;
};

export type MonitorEvent = {
  id: string; exchange: string; exchangeName: string; symbol: string; marketSymbol: string;
  announcementTitle: string | null; announcementUrl: string | null; publishedAt: string | null;
  firstDetectedAt: string; tradableAt: string | null; firstBid: number | null; firstAsk: number | null;
  monitorStartedAt: string; monitorEndsAt: string; monitorStatus: string; bookChecks: number;
  announcementMatched: boolean | number; marketMessage: string;
  pushedAt: string | null; updatedAt: string;
};

export type Announcement = {
  id: string; exchange: string; exchangeName: string; symbol: string | null; title: string; url: string;
  displayTitle?: string | null;
  publishedAt: string | null; scheduledAt: string | null; action: string;
  marketType: 'contract' | 'spot' | 'unknown'; fetchedAt: string;
  assetType?: 'stock' | 'crypto' | 'unknown';
  detailStatus?: 'pending' | 'parsed' | 'unavailable' | 'ambiguous';
  detailCheckedAt?: string | null; detailError?: string | null;
  detailSource?: 'official_api' | 'official_page' | 'verified_repair' | null;
};

export type ListingReminder = {
  action?: 'listing' | 'delisting'; openingSuspendsAt?: string | null;
  id: string; exchange: string; exchangeName: string; symbol: string; marketSymbol: string;
  marketType: 'contract' | 'spot'; scheduledAt: string | null; hasOccurred: boolean; marketStatus: string | null;
  contractKind?: 'perpetual' | 'delivery'; tradableAt?: string | null; recoveredAfterGap?: boolean;
  assetType: 'stock' | 'crypto' | 'unknown'; assetLabel: string | null;
  firstDetectedAt: string; announcementMatched: boolean; announcementTitle: string | null;
  announcementUrl: string | null; publishedAt: string | null;
};

export type ActivityLog = {
  id: string; at: string; level: 'info' | 'success' | 'warning' | 'error'; type: string;
  exchange: string | null; symbol: string | null; message: string;
};

export type MonitorSnapshot = {
  status: 'running' | 'degraded' | 'stopped'; updatedAt: string;
  schedule: { inventoryIntervalSeconds: number; newContractBookIntervalSeconds: number; newContractBookDurationMinutes: number };
  runtime: { lastCycleAt: string | null; lastContractScanAt: string | null; lastNewsScanAt: string | null; nextAlarmAt: string | null; lastError: string | null; activeBookMonitors: number };
  sources: SourceStatus[]; events: MonitorEvent[]; listingReminders: ListingReminder[]; announcements: Announcement[];
  pushLogs: Array<Record<string, unknown>>; activityLogs: ActivityLog[];
};
