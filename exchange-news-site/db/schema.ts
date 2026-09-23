import { integer, sqliteTable, text, uniqueIndex } from 'drizzle-orm/sqlite-core';

export const announcements = sqliteTable(
  'announcements',
  {
    id: integer('id').primaryKey({ autoIncrement: true }),
    announcementKey: text('announcement_key').notNull(),
    exchange: text('exchange').notNull(),
    exchangeName: text('exchange_name').notNull(),
    title: text('title').notNull(),
    url: text('url').notNull(),
    symbolsJson: text('symbols_json').notNull().default('[]'),
    marketType: text('market_type').notNull(),
    action: text('action').notNull(),
    assetType: text('asset_type').notNull().default('crypto'),
    publishedAt: text('published_at'),
    eventAt: text('event_at'),
    category: text('category'),
    contentHash: text('content_hash').notNull(),
    lastNotifiedHash: text('last_notified_hash'),
    status: text('status').notNull().default('active'),
    missingCount: integer('missing_count').notNull().default(0),
    firstSeenAt: text('first_seen_at').notNull(),
    lastSeenAt: text('last_seen_at').notNull(),
    updatedAt: text('updated_at').notNull(),
  },
  (table) => [uniqueIndex('idx_announcements_key').on(table.announcementKey)],
);

export const sourceStatus = sqliteTable('source_status', {
  exchange: text('exchange').primaryKey(),
  exchangeName: text('exchange_name').notNull(),
  status: text('status').notNull(),
  rawCount: integer('raw_count').notNull().default(0),
  activeCount: integer('active_count').notNull().default(0),
  durationMs: integer('duration_ms').notNull().default(0),
  message: text('message'),
  lastSuccessAt: text('last_success_at'),
  checkedAt: text('checked_at').notNull(),
});

export const pushLogs = sqliteTable('push_logs', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  announcementKey: text('announcement_key').notNull(),
  notificationHash: text('notification_hash').notNull(),
  kind: text('kind').notNull(),
  title: text('title').notNull(),
  body: text('body').notNull(),
  link: text('link'),
  status: text('status').notNull(),
  message: text('message'),
  createdAt: text('created_at').notNull(),
});

export const scanRuns = sqliteTable('scan_runs', {
  id: integer('id').primaryKey({ autoIncrement: true }),
  trigger: text('trigger').notNull(),
  status: text('status').notNull(),
  sourceOkCount: integer('source_ok_count').notNull().default(0),
  sourceErrorCount: integer('source_error_count').notNull().default(0),
  itemCount: integer('item_count').notNull().default(0),
  pushedCount: integer('pushed_count').notNull().default(0),
  seeded: integer('seeded', { mode: 'boolean' }).notNull().default(false),
  message: text('message'),
  startedAt: text('started_at').notNull(),
  finishedAt: text('finished_at').notNull(),
});

export const settings = sqliteTable('settings', {
  key: text('key').primaryKey(),
  value: text('value').notNull(),
  updatedAt: text('updated_at').notNull(),
});

export const inventory = sqliteTable(
  'inventory',
  {
    id: integer('id').primaryKey({ autoIncrement: true }),
    exchange: text('exchange').notNull(),
    symbol: text('symbol').notNull(),
    active: integer('active', { mode: 'boolean' }).notNull().default(true),
    missingCount: integer('missing_count').notNull().default(0),
    lastSeenAt: text('last_seen_at').notNull(),
  },
  (table) => [uniqueIndex('idx_inventory_exchange_symbol').on(table.exchange, table.symbol)],
);

export const scanState = sqliteTable('scan_state', {
  id: integer('id').primaryKey(),
  lockUntil: integer('lock_until').notNull().default(0),
  updatedAt: text('updated_at').notNull(),
});
