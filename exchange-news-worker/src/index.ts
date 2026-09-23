// @ts-nocheck -- recovered from the verified 2026-09-12 Cloudflare deployment bundle.
var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// src/index.ts
import { DurableObject } from "cloudflare:workers";
import { BOOK_HIGH_FREQUENCY_MS, classifyBookMonitor } from "./book-monitor-policy";

// src/state-storage.ts
var PREFIX = "state:v3:";
var CHUNK_SIZE = 16e3;
async function getMany(storage, keys) {
  const result = /* @__PURE__ */ new Map();
  for (let offset = 0; offset < keys.length; offset += 100) {
    for (const [key, value] of await storage.get(keys.slice(offset, offset + 100))) result.set(key, value);
  }
  return result;
}
__name(getMany, "getMany");
function joinParts(values, base, count) {
  let result = "";
  for (let i = 0; i < count; i++) {
    const part = values.get(base + i);
    if (typeof part !== "string") throw new Error("\u6301\u4E45\u5316\u72B6\u6001\u5206\u7247\u7F3A\u5931\uFF0C\u7981\u6B62\u91CD\u5EFA\u7A7A\u57FA\u7EBF");
    result += part;
  }
  return result;
}
__name(joinParts, "joinParts");
async function readState(storage, cache = /* @__PURE__ */ new Map()) {
  const countValue = await storage.get(PREFIX + "parts");
  if (countValue !== void 0) {
    const count = Number(countValue);
    if (!Number.isInteger(count) || count < 1 || count > 1e4) throw new Error("\u6301\u4E45\u5316\u72B6\u6001\u7D22\u5F15\u5F02\u5E38\uFF0C\u7981\u6B62\u91CD\u5EFA\u7A7A\u57FA\u7EBF");
    const manifestParts = await getMany(storage, Array.from({ length: count }, (_, i) => PREFIX + "manifest:" + i));
    const manifest = JSON.parse(joinParts(manifestParts, PREFIX + "manifest:", count));
    const keys = Object.values(manifest).flatMap((section) => section.records.flatMap(([, base, parts]) => Array.from({ length: parts }, (_, i) => base + i)));
    const records = await getMany(storage, keys);
    const restored = {};
    for (const [field, section] of Object.entries(manifest)) {
      const values = section.records.map(([name, base, parts]) => [name, JSON.parse(joinParts(records, base, parts))]);
      restored[field] = section.shape === "array" ? values.map(([, value]) => value) : section.shape === "object" ? Object.fromEntries(values) : values[0]?.[1];
    }
    cache.clear();
    for (const [key, value] of [...manifestParts, ...records]) cache.set(key, value);
    cache.set(PREFIX + "parts", countValue);
    return restored;
  }
  const oldCount = await storage.get("state:parts");
  if (!oldCount) return null;
  const oldParts = await getMany(storage, Array.from({ length: oldCount }, (_, i) => "state:part:" + i));
  return JSON.parse(joinParts(oldParts, "state:part:", oldCount));
}
__name(readState, "readState");
async function writeState(storage, state, cache = /* @__PURE__ */ new Map()) {
  const entries = /* @__PURE__ */ new Map();
  const manifest = {};
  const add = /* @__PURE__ */ __name((base, value) => {
    const serialized = JSON.stringify(value);
    let count2 = 0;
    for (let offset = 0; offset < serialized.length; offset += CHUNK_SIZE) entries.set(base + count2++, serialized.slice(offset, offset + CHUNK_SIZE));
    return count2;
  }, "add");
  const snapshot = JSON.parse(JSON.stringify(state));
  for (const [field, value] of Object.entries(snapshot)) {
    if (value === void 0) continue;
    const shape = Array.isArray(value) ? "array" : field === "inventory" ? "object" : "value";
    const records = Array.isArray(value) ? value.map((row, index) => [
      String(row?.key ?? row?.id ?? (field === "newsHourlyStats" ? row.exchange + ":" + row.hour : index)),
      row
    ]) : shape === "object" && value && typeof value === "object" ? Object.entries(value) : [["value", value]];
    const used = /* @__PURE__ */ new Set();
    manifest[field] = { shape, records: await Promise.all(records.map(async ([name, row], index) => {
      let identity = String(name);
      if (used.has(identity)) identity += ":duplicate:" + index;
      used.add(identity);
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(field + ":" + identity));
      const hash = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
      const base = PREFIX + "record:" + hash + ":";
      return [String(name), base, add(base, row)];
    })) };
  }
  const count = add(PREFIX + "manifest:", manifest);
  entries.set(PREFIX + "parts", String(count));
  const changed = [...entries].filter(([key, value]) => cache.get(key) !== value);
  const retired = [...cache.keys()].filter((key) => !entries.has(key));
  if (!changed.length && !retired.length) return 0;
  await storage.transaction(async (transaction) => {
    for (let offset = 0; offset < changed.length; offset += 100) await transaction.put(Object.fromEntries(changed.slice(offset, offset + 100)));
    for (let offset = 0; offset < retired.length; offset += 100) await transaction.delete(retired.slice(offset, offset + 100));
  });
  cache.clear();
  for (const [key, value] of entries) cache.set(key, value);
  return changed.length + retired.length;
}
__name(writeState, "writeState");

// src/index.ts
var CLOUD_LEASE_MS = 18e4;
async function executionAllowed(env) {
  if (env.LOCAL_RUNTIME === "1" || !env.CONTROL_TOKEN) return true;
  const lease = await env.STATE_KV.get("execution-lease:v1", "json");
  return Boolean(lease && lease.until > Date.now() && lease.until <= Date.now() + CLOUD_LEASE_MS);
}
__name(executionAllowed, "executionAllowed");
var EXCHANGES = [
  { code: "bn", name: "Binance" },
  { code: "bg", name: "Bitget" },
  { code: "by", name: "Bybit" },
  { code: "gate", name: "Gate" },
  { code: "okx", name: "OKX" },
  { code: "aster", name: "Aster" },
  { code: "hl", name: "Hyperliquid" }
];
var INVENTORY_INTERVAL_MS = 15e3;
var NEWS_INTERVAL_MS = 6e4;
var BOOK_INTERVAL_MS = 1e3;
var BOOK_MONITOR_MS = BOOK_HIGH_FREQUENCY_MS;
var BOOK_RECHECK_MS = 3e4;
var PUSH_RETRY_LIMIT = 6;
var REPAIR_VERSION = 3;
var HISTORICAL_NOTIFICATION_AGE_MS = 2 * 60 * 6e4;
var PUSH_DEDUP_MS = 7 * 24 * 60 * 6e4;
var PUSH_SENDING_STALE_MS = 2 * 6e4;
var PRIMARY_MONITOR_NAME = "global-exchange-monitor-apac-se-v6";
var NEWS_RAW_LOG_LIMIT = 500;
var NEWS_HOURLY_RETENTION_MS = 25 * 60 * 6e4;
var PUSH_LOCK_PREFIX = "push-lock:";
var USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36";
function retryAfterMs(response) {
  const value = response?.headers.get("retry-after");
  if (!value) return 0;
  const seconds = Number(value);
  const milliseconds = Number.isFinite(seconds) ? seconds * 1e3 : Date.parse(value) - Date.now();
  return Number.isFinite(milliseconds) ? Math.max(0, milliseconds) : 0;
}
__name(retryAfterMs, "retryAfterMs");
var UpstreamHttpError = class extends Error {
  constructor(status, retryAfterMs2, message) {
    super(message);
    this.status = status;
    this.retryAfterMs = retryAfterMs2;
  }
  status;
  retryAfterMs;
  static {
    __name(this, "UpstreamHttpError");
  }
};
async function upstreamHttpError(response) {
  let detail = "";
  if (response.headers.get("content-type")?.includes("json")) {
    try {
      const body = await response.json();
      const message = body.message ?? body.msg ?? body.detail ?? body.error;
      if (typeof message === "string") detail = message.replace(/\s+/g, " ").slice(0, 180);
    } catch {
    }
  } else {
    await response.body?.cancel();
  }
  return new UpstreamHttpError(
    response.status,
    retryAfterMs(response),
    `${response.status} ${response.statusText}${detail ? ": " + detail : ""}`
  );
}
__name(upstreamHttpError, "upstreamHttpError");
function json(value, status = 200, headers = {}) {
  const outgoing = new Headers(headers);
  outgoing.set("cache-control", "no-store");
  return Response.json(value, {
    status,
    headers: outgoing
  });
}
__name(json, "json");
function exchangeName(code) {
  if (code === "bnus") return "Binance.US";
  return EXCHANGES.find((row) => row.code === code)?.name ?? code;
}
__name(exchangeName, "exchangeName");
function asNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}
__name(asNumber, "asNumber");
function asIso(value) {
  if (value === null || value === void 0 || value === "") return null;
  let number = Number(value);
  if (Number.isFinite(number)) {
    if (number < 1e10) number *= 1e3;
    const date2 = new Date(number);
    return Number.isNaN(date2.valueOf()) ? null : date2.toISOString();
  }
  const date = new Date(String(value));
  return Number.isNaN(date.valueOf()) ? null : date.toISOString();
}
__name(asIso, "asIso");
function binanceCmsPublishedAt(value) {
  const published = asIso(value);
  if (!published) return null;
  return new Date(Date.parse(published) - 8 * 60 * 6e4).toISOString();
}
__name(binanceCmsPublishedAt, "binanceCmsPublishedAt");
function beijingDateKey(value) {
  const timestamp = typeof value === "number" ? value : Date.parse(value);
  if (!Number.isFinite(timestamp)) return null;
  return new Date(timestamp + 8 * 60 * 6e4).toISOString().slice(0, 10);
}
__name(beijingDateKey, "beijingDateKey");
function canonicalAnnouncementUrl(exchange, url) {
  if (exchange === "gate") {
    const id = url.match(/^https?:\/\/www\.gate\.com\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?announcements\/article\/(\d+)(?:[/?#]|$)/i)?.[1];
    return id ? `https://www.gate.com/zh/announcements/article/${id}` : url;
  }
  if (exchange !== "bn") return url;
  const articleCode = url.match(/([a-f0-9]{32})(?:\/|$)/i)?.[1];
  return articleCode ? `https://www.binance.com/zh-CN/support/announcement/detail/${articleCode}` : url;
}
__name(canonicalAnnouncementUrl, "canonicalAnnouncementUrl");
function announcementIdentityKey(input) {
  const base = canonicalAnnouncementUrl(input.exchange, input.url) || input.title;
  return input.symbol ? `${input.exchange}:${base}:${input.symbol}` : `${input.exchange}:${base}`;
}
__name(announcementIdentityKey, "announcementIdentityKey");
function traceEndpoint(url) {
  try {
    const parsed = new URL(url);
    return `${parsed.host}${parsed.pathname}`;
  } catch {
    return url;
  }
}
__name(traceEndpoint, "traceEndpoint");
function recordHttpAttempt(trace, url, requestStartedAt, response, error = null) {
  if (!trace) return;
  trace.attempts.push({
    requestStartedAt,
    responseCompletedAt: (/* @__PURE__ */ new Date()).toISOString(),
    endpoint: traceEndpoint(url),
    statusCode: response?.status ?? null,
    retryAfterMs: retryAfterMs(response),
    error: error ? String(error) : null
  });
}
__name(recordHttpAttempt, "recordHttpAttempt");
function collectArticleText(value, out = []) {
  if (!value || typeof value !== "object") return out;
  if (Array.isArray(value)) {
    for (const child of value) collectArticleText(child, out);
    return out;
  }
  const row = value;
  if (row.node === "text" && typeof row.text === "string") out.push(row.text);
  for (const child of Object.values(row)) collectArticleText(child, out);
  return out;
}
__name(collectArticleText, "collectArticleText");
function textIncludesAny(text, words) {
  const normalized = text.toLowerCase();
  return words.some((word) => normalized.includes(word));
}
__name(textIncludesAny, "textIncludesAny");
function classifyAction(title, fallback = "unknown") {
  if (textIncludesAny(title, ["delist", "remove", "terminate", "\u4E0B\u67B6", "\u79FB\u9664", "\u505C\u6B62\u4EA4\u6613"])) return "delisting";
  if (textIncludesAny(title, ["suspend trading", "trading suspension", "\u6682\u505C\u4EA4\u6613", "\u4EA4\u6613\u6682\u505C"])) return "suspension";
  if (textIncludesAny(title, ["resume trading", "trading resumes", "\u6062\u590D\u4EA4\u6613", "\u91CD\u65B0\u5F00\u653E\u4EA4\u6613"])) return "resumption";
  if (textIncludesAny(title, ["migration", "ticker change", "symbol change", "rename", "token swap", "\u8FC1\u79FB", "\u66F4\u540D", "\u7F6E\u6362"])) return "migration";
  if (textIncludesAny(title, ["funding rate", "funding interval", "risk limit", "tick size", "\u8D44\u91D1\u8D39", "\u5009\u4F4D\u9650\u984D", "\u4ED3\u4F4D\u9650\u989D", "\u4EF7\u683C\u7CBE\u5EA6", "\u5408\u7EA6\u9762\u503C"])) return "parameter_change";
  if (textIncludesAny(title, ["earn", "convert", "loan", "\u7406\u8D22", "\u7406\u8CA1", "\u4F59\u5E01\u5B9D", "\u9918\u5E63\u5BF6", "\u5145\u503C", "\u5145\u5E63"]) && classifyMarket(title) === "unknown") return "product_update";
  if (textIncludesAny(title, ["will list", "to list", "new listing", "new perp listing", "launch", "opens", "\u4E0A\u7EBF", "\u4E0A\u7DDA", "\u4E0A\u67B6", "\u65B0\u589E", "\u5F00\u653E\u4EA4\u6613", "\u958B\u653E\u4EA4\u6613"])) return "listing";
  return fallback;
}
__name(classifyAction, "classifyAction");
function classifyMarket(title, fallback = "unknown") {
  if (textIncludesAny(title, ["futures", "perpetual", "perp", "contract", "usdt-m", "usd-m", "\u5408\u7EA6", "\u5408\u7D04", "\u6C38\u7EED", "\u6C38\u7E8C", "\u671F\u8D27", "\u671F\u8CA8", "u\u672C\u4F4D"])) return "contract";
  if (textIncludesAny(title, ["spot", "trading pair", "\u73B0\u8D27", "\u73FE\u8CA8", "\u5E01\u5BF9", "\u5E63\u5C0D"])) return "spot";
  return fallback;
}
__name(classifyMarket, "classifyMarket");
var SYMBOL_EXCLUDE = /* @__PURE__ */ new Set([
  "API",
  "ETF",
  "IPO",
  "USD",
  "USDT",
  "USDC",
  "BINANCE",
  "BITGET",
  "BYBIT",
  "GATE",
  "OKX",
  "ASTER",
  "DEFI",
  "PERP",
  "PERPETUAL",
  "FUTURE",
  "FUTURES",
  "CONTRACT",
  "CONTRACTS",
  "SPOT",
  "TRADING",
  "LIST",
  "LAUNCH",
  "THE",
  "AND",
  "FOR",
  "WITH",
  "MULTIPLE",
  "UNIFIED",
  "BIG",
  "NEW",
  "ON",
  "EQUITY",
  "EQUITIES",
  "TRADFI",
  "MARGIN",
  "VIP",
  "BOT",
  "BOTS",
  "UTC",
  "GMT",
  "UP",
  "TO",
  "SUPPORT"
]);
function assetTypeFromEvidence(text, symbols = []) {
  const lower = text.toLowerCase();
  if (["\u80A1\u7968", "\u7F8E\u80A1", "\u6E2F\u80A1", "\u80A1\u672C", "tradfi", "pre-ipo", "pre ipo"].some((word) => lower.includes(word)) || /(^|[^a-z])(?:stocks?|equities|equity|cfds?)(?=$|[^a-z])/.test(lower) || symbols.some((symbol) => symbol.toUpperCase().endsWith("STOCK"))) return "stock";
  return symbols.length ? "crypto" : "unknown";
}
__name(assetTypeFromEvidence, "assetTypeFromEvidence");
function assetTypeFromMetadata(row, symbol) {
  const subtypes = Array.isArray(row.underlyingSubType) ? row.underlyingSubType.map((value) => String(value).toUpperCase()) : [];
  const tags = Array.isArray(row.tags) ? row.tags.map((value) => String(value).toLowerCase()) : [];
  const symbolType = String(row.symbolType ?? row.assetType ?? "").toLowerCase();
  const channel = String(row.channel ?? row.marketChannel ?? "").toLowerCase();
  const contractType = String(row.contractType ?? row.contract_type ?? "").toLowerCase();
  const metadataText = [
    row.category,
    row.businessType,
    row.productType,
    row.underlying,
    row.uly,
    row.instFamily,
    row.displayName,
    row.name,
    symbolType,
    channel,
    contractType,
    ...subtypes,
    ...tags
  ].filter(Boolean).join(" ");
  if (symbolType === "stock" || subtypes.includes("STOCK") || ["nasdaq", "hkstock", "krstock", "cnstock"].includes(channel) || tags.some((value) => ["stock", "stocks", "\u80A1\u7968"].includes(value)) || contractType.includes("tradifi") || contractType.includes("tradfi") || contractType.includes("stock")) return "stock";
  return assetTypeFromEvidence(metadataText, [symbol]);
}
__name(assetTypeFromMetadata, "assetTypeFromMetadata");
function mergedAssetType(...types) {
  if (types.includes("stock")) return "stock";
  if (types.includes("crypto")) return "crypto";
  return "unknown";
}
__name(mergedAssetType, "mergedAssetType");
function knownAnnouncementAssetType(announcements, symbol) {
  return announcements.some(
    (row) => row.assetType === "stock" && (row.symbol === symbol || row.symbols?.includes(symbol) || extractSymbols(row.title).includes(symbol))
  ) ? "stock" : "unknown";
}
__name(knownAnnouncementAssetType, "knownAnnouncementAssetType");
function chineseAnnouncementTitle(announcement, symbol) {
  if (!announcement) return null;
  const title = announcement.title.replace(/\s+/g, " ").trim();
  if (/[\u3400-\u4dbf\u4e00-\u9fff]/.test(title)) return title;
  const market = announcement.marketType === "spot" ? "\u73B0\u8D27" : announcement.marketType === "contract" ? "\u5408\u7EA6" : "";
  if (/delay|postpone|reschedule/i.test(title)) return `${symbol} ${market}\u4E0A\u7EBF\u5EF6\u671F\u516C\u544A`;
  const actionLabels = {
    listing: "\u4E0A\u7EBF\u516C\u544A",
    delisting: "\u4E0B\u67B6\u516C\u544A",
    suspension: "\u6682\u505C\u4EA4\u6613\u516C\u544A",
    resumption: "\u6062\u590D\u4EA4\u6613\u516C\u544A",
    migration: "\u6539\u540D/\u8FC1\u79FB\u516C\u544A",
    parameter_change: "\u53C2\u6570\u53D8\u66F4\u516C\u544A",
    product_update: "\u7406\u8D22/\u5151\u6362/\u501F\u8D37\u7B49\u4EA7\u54C1\u516C\u544A"
  };
  return `${symbol} ${market}${actionLabels[announcement.action] ?? "\u4EA4\u6613\u6240\u516C\u544A"}`;
}
__name(chineseAnnouncementTitle, "chineseAnnouncementTitle");
function projectNameAliases(title) {
  const aliases = /* @__PURE__ */ new Set();
  for (const match of title.matchAll(/\b([A-Za-z][A-Za-z0-9]{1,23})\s*[（(]([A-Z][A-Z0-9]{1,23})[）)]/g)) {
    const name = match[1].toUpperCase(), ticker = match[2];
    if (name !== ticker && !SYMBOL_EXCLUDE.has(ticker) && !/^\d*X$/.test(ticker) && !new RegExp(`\\b${name}[/_-]USDT\\b`, "i").test(title)) aliases.add(name);
  }
  return aliases;
}
__name(projectNameAliases, "projectNameAliases");
function extractSymbols(title) {
  const upper = title.toUpperCase();
  const symbols = /* @__PURE__ */ new Set();
  const patterns = [
    /(?:^|[^A-Z0-9])([A-Z0-9]{1,24})(?:\/|-|_)?USDT(?:[^A-Z0-9]|$)/g,
    /\$([A-Z0-9]{1,24})/g,
    /[（(]([A-Z][A-Z0-9]{1,23})[）)]/g,
    /(?:LIST|LAUNCH(?:ES)?|DELIST|REMOVE)\s+([A-Z][A-Z0-9]{0,23})(?=\s|$|[^A-Z0-9])/g,
    /(?:上线|上線|上架|新增|下架|移除)\s*([A-Z][A-Z0-9]{0,23})(?=\s|$|[^A-Z0-9])/g,
    /(?:FUTURES?\s+FOR|PERPETUALS?\s+FOR)\s+([A-Z][A-Z0-9]{0,23})(?=\s|$|[^A-Z0-9])/g
  ];
  for (const [patternIndex, pattern] of patterns.entries()) {
    for (const match of upper.matchAll(pattern)) {
      const symbol = match[1]?.replace(/[^A-Z0-9]/g, "").replace(/USDT$/, "");
      const excluded = patternIndex < 2 ? ["USD", "USDT", "USDC"].includes(symbol) : SYMBOL_EXCLUDE.has(symbol);
      if (symbol && !excluded && !/^\d+X$/.test(symbol)) symbols.add(symbol);
    }
  }
  const chinesePair = title.match(/([\u3400-\u4dbf\u4e00-\u9fff]{1,16})\s*(?:[/_\-(（]\s*)?USDT/i);
  if (chinesePair?.[1]) symbols.add(chinesePair[1]);
  for (const match of title.matchAll(/(?:^|[\s、,/])([A-Z][A-Z0-9]{0,23})(?=\s*(?:、|,|\/|合约|合約|现货|現貨|equity|equities))/g)) {
    const symbol = match[1].replace(/USDT$/, "");
    if (symbol && !SYMBOL_EXCLUDE.has(symbol)) symbols.add(symbol);
  }
  const aliases = projectNameAliases(title);
  return [...symbols].filter((symbol) => !aliases.has(symbol));
}
__name(extractSymbols, "extractSymbols");
function extractSymbol(title) {
  return extractSymbols(title)[0] ?? null;
}
__name(extractSymbol, "extractSymbol");
function titleHasSymbol(title, symbol) {
  const escaped = symbol.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^A-Z0-9])${escaped}(?=(?:[/_\\- ]?USDT)|[^A-Z0-9]|$)`, "i").test(title);
}
__name(titleHasSymbol, "titleHasSymbol");
async function fetchJson(url, init = {}, timeoutMs = 8e3, trace) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), timeoutMs);
  const requestStartedAt = (/* @__PURE__ */ new Date()).toISOString();
  let response = null;
  try {
    response = await fetch(url, {
      ...init,
      headers: { accept: "application/json,text/html;q=0.9,*/*;q=0.8", "user-agent": USER_AGENT, ...init.headers ?? {} },
      signal: controller.signal
    });
    if (!response.ok) throw await upstreamHttpError(response);
    const data = await response.json();
    if (data && typeof data === "object") {
      const result = data;
      const code = result.retCode ?? result.code;
      if (code !== void 0 && !["0", "00000", "000000", "200", "200000"].includes(String(code))) {
        throw new Error(`\u63A5\u53E3\u4E1A\u52A1\u9519\u8BEF ${String(code)}: ${String(result.retMsg ?? result.msg ?? result.message ?? "")}`);
      }
    }
    recordHttpAttempt(trace, url, requestStartedAt, response);
    return data;
  } catch (error) {
    recordHttpAttempt(trace, url, requestStartedAt, response, error);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
__name(fetchJson, "fetchJson");
async function fetchJsonFallback(urls, init = {}, timeoutMs = 8e3, trace) {
  const errors = [];
  for (const url of urls) {
    try {
      return await fetchJson(url, init, timeoutMs, trace);
    } catch (error) {
      errors.push(`${new URL(url).host}: ${String(error)}`);
    }
  }
  throw new Error(errors.join(" | "));
}
__name(fetchJsonFallback, "fetchJsonFallback");
async function fetchText(url, timeoutMs = 8e3, trace) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), timeoutMs);
  const requestStartedAt = (/* @__PURE__ */ new Date()).toISOString();
  let response = null;
  try {
    response = await fetch(url, {
      headers: { "user-agent": USER_AGENT, "cache-control": "no-cache" },
      signal: controller.signal
    });
    if (!response.ok) throw await upstreamHttpError(response);
    const text = await response.text();
    recordHttpAttempt(trace, url, requestStartedAt, response);
    return text;
  } catch (error) {
    recordHttpAttempt(trace, url, requestStartedAt, response, error);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
__name(fetchText, "fetchText");
async function collectWebSocketJson(url, settleMs = 0, timeoutMs = 5e3) {
  return await new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    const rows = [];
    let settled = false;
    let settleTimer = null;
    const finish = /* @__PURE__ */ __name((error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      if (settleTimer) clearTimeout(settleTimer);
      try {
        socket.close(1e3, "snapshot complete");
      } catch {
      }
      if (error && !rows.length) reject(error);
      else resolve(rows);
    }, "finish");
    const timeout = setTimeout(() => finish(new Error("websocket timeout")), timeoutMs);
    socket.addEventListener("message", (event) => {
      try {
        const text = typeof event.data === "string" ? event.data : new TextDecoder().decode(event.data);
        const payload = JSON.parse(text);
        const body = payload?.data ?? payload;
        rows.push(...Array.isArray(body) ? body : [body]);
        if (!settleMs) finish();
        else if (!settleTimer) settleTimer = setTimeout(() => finish(), settleMs);
      } catch (error) {
        finish(error);
      }
    });
    socket.addEventListener("error", () => finish(new Error("websocket connection failed")));
    socket.addEventListener("close", () => finish(new Error("websocket closed before data")));
  });
}
__name(collectWebSocketJson, "collectWebSocketJson");
async function collectWebSocketJsonRetry(url, settleMs = 0, timeoutMs = 5e3, attempts = 3) {
  const errors = [];
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await collectWebSocketJson(url, settleMs, timeoutMs);
    } catch (error) {
      errors.push(`attempt ${attempt}: ${String(error)}`);
      if (attempt < attempts) await new Promise((resolve) => setTimeout(resolve, attempt * 350));
    }
  }
  throw new Error(errors.join(" | "));
}
__name(collectWebSocketJsonRetry, "collectWebSocketJsonRetry");
async function collectWebSocketJsonFallback(urls, settleMs = 0, timeoutMs = 5e3, attemptsPerUrl = 2) {
  const errors = [];
  for (const url of urls) {
    try {
      return await collectWebSocketJsonRetry(url, settleMs, timeoutMs, attemptsPerUrl);
    } catch (error) {
      errors.push(`${new URL(url).pathname}: ${String(error)}`);
    }
  }
  throw new Error(errors.join(" | "));
}
__name(collectWebSocketJsonFallback, "collectWebSocketJsonFallback");
async function requestWebSocketSnapshot(urls, request, timeoutMs = 6e3) {
  const errors = [];
  for (const url of urls) {
    try {
      return await new Promise((resolve, reject) => {
        const socket = new WebSocket(url);
        socket.binaryType = "arraybuffer";
        let settled = false;
        const finish = /* @__PURE__ */ __name((rows, error) => {
          if (settled) return;
          settled = true;
          clearTimeout(timeout);
          try {
            socket.close(1e3, "snapshot complete");
          } catch {
          }
          if (rows?.length) resolve(rows);
          else reject(error ?? new Error("empty websocket snapshot"));
        }, "finish");
        const timeout = setTimeout(() => finish(void 0, new Error("websocket timeout")), timeoutMs);
        socket.addEventListener("open", () => socket.send(JSON.stringify(request)));
        socket.addEventListener("message", (event) => {
          try {
            const text = typeof event.data === "string" ? event.data : new TextDecoder().decode(event.data);
            const payload = JSON.parse(text);
            if (Array.isArray(payload?.data) && payload.data.length) finish(payload.data);
            else if (payload?.event === "error") finish(void 0, new Error(payload?.msg ?? payload?.code ?? "subscription error"));
          } catch (error) {
            finish(void 0, error);
          }
        });
        socket.addEventListener("error", () => finish(void 0, new Error("websocket connection failed")));
        socket.addEventListener("close", () => finish(void 0, new Error("websocket closed before data")));
      });
    } catch (error) {
      errors.push(`${new URL(url).host}: ${String(error)}`);
    }
  }
  throw new Error(errors.join(" | "));
}
__name(requestWebSocketSnapshot, "requestWebSocketSnapshot");
function scriptJson(html, id) {
  const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = html.match(new RegExp(`<script[^>]+id=["']${escaped}["'][^>]*>([\\s\\S]*?)<\\/script>`, "i"));
  if (!match?.[1]) throw new Error(`missing ${id}`);
  return JSON.parse(match[1]);
}
__name(scriptJson, "scriptJson");
function scriptVariableJson(html, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = html.match(new RegExp(`${escaped}\\s*=\\s*([\\s\\S]*?)<\\/script>`, "i"));
  if (!match?.[1]) throw new Error(`missing ${name}`);
  return JSON.parse(match[1].trim().replace(/;$/, ""));
}
__name(scriptVariableJson, "scriptVariableJson");
function walkObjects(value, out = []) {
  if (!value || typeof value !== "object") return out;
  if (Array.isArray(value)) {
    for (const item of value) walkObjects(item, out);
    return out;
  }
  const row = value;
  if (typeof row.title === "string") out.push(row);
  for (const child of Object.values(row)) walkObjects(child, out);
  return out;
}
__name(walkObjects, "walkObjects");
function announcementRow(exchange, title, url, publishedAt, fallbackAction = "unknown", fallbackMarket = "unknown", scheduledAt = null) {
  const cleanTitle = title.replace(/\u0000/g, "").replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  if (!cleanTitle) return null;
  const action = classifyAction(cleanTitle, fallbackAction);
  if (action === "unknown") return null;
  const marketType = classifyMarket(cleanTitle, fallbackMarket);
  const published = asIso(publishedAt);
  const canonicalUrl = canonicalAnnouncementUrl(exchange, url);
  const symbol = extractSymbol(cleanTitle);
  const row = {
    exchange,
    exchangeName: exchangeName(exchange),
    key: "",
    title: cleanTitle,
    url: canonicalUrl,
    publishedAt: published,
    scheduledAt,
    action,
    marketType,
    symbol,
    symbols: extractSymbols(cleanTitle),
    assetType: assetTypeFromEvidence(cleanTitle, symbol ? [symbol] : [])
  };
  row.key = announcementIdentityKey(row);
  return row;
}
__name(announcementRow, "announcementRow");
function mcpPublishedAt(article) {
  const localText = String(article.create_time ?? article.createTime ?? "");
  const localMatch = localText.match(/^(20\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$/);
  const published = localMatch ? new Date(Date.UTC(
    Number(localMatch[1]),
    Number(localMatch[2]) - 1,
    Number(localMatch[3]),
    Number(localMatch[4]) - 8,
    Number(localMatch[5]),
    Number(localMatch[6])
  )).toISOString() : asIso(article.publish_time ?? article.publishTime);
  if (!published) return null;
  const title = String(article.title ?? "");
  const dateMatch = title.match(/(?:\(|\b)(20\d{2})[-/.\u5e74](\d{1,2})[-/.\u6708](\d{1,2})(?:\u65e5|\)|\b)/);
  if (!dateMatch) return published;
  const titleDay = Date.UTC(Number(dateMatch[1]), Number(dateMatch[2]) - 1, Number(dateMatch[3]));
  const publishedDay = new Date(published);
  publishedDay.setUTCHours(0, 0, 0, 0);
  return publishedDay.valueOf() - titleDay > 2 * 864e5 ? null : published;
}
__name(mcpPublishedAt, "mcpPublishedAt");
async function fetchGateMcpNews(exchange, code, trace) {
  const endpoint = "https://api.gatemcp.ai/mcp/news";
  const commonHeaders = {
    accept: "application/json, text/event-stream",
    "content-type": "application/json",
    "user-agent": USER_AGENT
  };
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), 1e4);
  try {
    const initStartedAt = (/* @__PURE__ */ new Date()).toISOString();
    let initResponse = null;
    try {
      initResponse = await fetch(endpoint, {
        method: "POST",
        headers: commonHeaders,
        signal: controller.signal,
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: {
            protocolVersion: "2025-06-18",
            capabilities: {},
            clientInfo: { name: "exchange-news-monitor", version: "1.0.0" }
          }
        })
      });
      recordHttpAttempt(trace, endpoint, initStartedAt, initResponse);
    } catch (error) {
      recordHttpAttempt(trace, endpoint, initStartedAt, initResponse, error);
      throw error;
    }
    if (!initResponse?.ok) throw new Error(`Gate MCP initialize: ${initResponse?.status ?? "no response"}`);
    const sessionId = initResponse.headers.get("mcp-session-id");
    await initResponse.text();
    if (!sessionId) throw new Error("Gate MCP initialize: missing session id");
    const requestStartedAt = (/* @__PURE__ */ new Date()).toISOString();
    let response = null;
    try {
      response = await fetch(endpoint, {
        method: "POST",
        signal: controller.signal,
        headers: { ...commonHeaders, "mcp-session-id": sessionId },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: 2,
          method: "tools/call",
          params: {
            name: "news_feed_get_exchange_announcements",
            arguments: { exchange, announcement_type: "all", limit: 100 }
          }
        })
      });
      recordHttpAttempt(trace, endpoint, requestStartedAt, response);
    } catch (error) {
      recordHttpAttempt(trace, endpoint, requestStartedAt, response, error);
      throw error;
    }
    if (!response?.ok) throw new Error(`Gate MCP announcements: ${response?.status ?? "no response"}`);
    const data = await response.json();
    if (data?.error) throw new Error(`Gate MCP announcements: ${data.error.message ?? "unknown error"}`);
    let structured = data?.result?.structuredContent;
    if (!structured) {
      const text = data?.result?.content?.find((row) => row?.type === "text")?.text;
      if (text) structured = JSON.parse(text);
    }
    const rows = Array.isArray(structured?.items) ? structured.items : [];
    const mapped = rows.map((article) => {
      const noticeType = String(article.notice_type ?? "");
      const fallbackAction = noticeType === "1" ? "listing" : noticeType === "2" ? "delisting" : "unknown";
      const base = announcementRow(
        code,
        article.title ?? "",
        article.url ?? "",
        mcpPublishedAt(article),
        fallbackAction
      );
      if (!base) return [];
      return [base];
    });
    return mapped.flat();
  } finally {
    clearTimeout(timeout);
  }
}
__name(fetchGateMcpNews, "fetchGateMcpNews");
async function fetchBinanceNews(trace) {
  const categories = [[48, "listing"], [161, "delisting"], [49, "unknown"]];
  const results = await Promise.allSettled(categories.map(async ([id, action]) => {
    const query = new URLSearchParams({ type: "1", pageNo: "1", pageSize: "20", catalogId: String(id) });
    const data = await fetchJson(`https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?${query}`, { headers: { lang: "zh-CN", clienttype: "web" } }, 4e3, trace);
    const catalogs = data?.data?.catalogs;
    if (!Array.isArray(catalogs) || !catalogs.some((c) => Array.isArray(c.articles) && c.articles.length)) throw new Error(`Binance catalogue ${id} empty`);
    return catalogs.flatMap((c) => (c.articles ?? []).flatMap((article) => {
      if (!/^[a-f0-9]{32}$/i.test(String(article.code ?? ""))) return [];
      const row = announcementRow("bn", article.title ?? "", `https://www.binance.com/zh-CN/support/announcement/detail/${article.code}`, binanceCmsPublishedAt(article.releaseDate), action);
      return row ? [row] : [];
    }));
  }));
  const rows = results.flatMap((result) => result.status === "fulfilled" ? result.value : []);
  if (results.every((result) => result.status === "fulfilled")) return deduplicateCatalogue(rows);
  const mirror = await fetchGateMcpNews("binance", "bn", trace);
  return deduplicateCatalogue([...mirror, ...rows]);
}
__name(fetchBinanceNews, "fetchBinanceNews");
async function fetchBybitNews(trace) {
  const categories = [
    ["listing", "new_crypto"],
    ["delisting", "delistings"],
    ["unknown", "product_updates"],
    ["unknown", "maintenance_updates"]
  ];
  const batches = await Promise.all(categories.map(async ([fallback, type]) => {
    const query = new URLSearchParams({ locale: "zh-TW", type, limit: "30" });
    const path = `/v5/announcements/index?${query}`;
    const data = await fetchJsonFallback([`https://api.bytick.com${path}`, `https://api.bybit.com${path}`], {}, 8e3, trace);
    return (data?.result?.list ?? []).map((article) => announcementRow(
      "by",
      article.title ?? "",
      article.url ?? "https://www.bybit.com/en/announcement-info/",
      article.publishTime ?? article.dateTimestamp,
      fallback
    )).filter(Boolean);
  }));
  return batches.flat();
}
__name(fetchBybitNews, "fetchBybitNews");
async function fetchAsterNews(trace) {
  const categories = [["listing", "NEW_LISTING"], ["delisting", "DELISTING"]];
  const batches = await Promise.all(categories.map(async ([fallback, category]) => {
    const data = await fetchJson("https://www.asterdex.com/bapi/composite/v1/public/composite/ae/announcement/search", {
      method: "POST",
      headers: { "content-type": "application/json", lang: "en" },
      body: JSON.stringify({ category, page: 1, size: 50 })
    }, 8e3, trace);
    return (data?.data?.rows ?? []).map((article) => announcementRow(
      "aster",
      article.title ?? "",
      `https://www.asterdex.com/en/announcement/${article.id ?? article.announcementId ?? ""}?category=${category}`,
      article.publishTime ?? article.updateTime ?? article.time,
      fallback,
      "contract"
    )).filter(Boolean);
  }));
  return batches.flat();
}
__name(fetchAsterNews, "fetchAsterNews");
async function fetchBitgetNews(trace) {
  const sections = [
    ["listing", "spot", "5955813039257"],
    ["listing", "contract", "12508313405000"],
    ["delisting", "unknown", "12508313443290"],
    ["unknown", "unknown", "12508313443483"]
  ];
  const batches = await Promise.all(sections.map(async ([fallback, market, section]) => {
    const source = `https://www.bitget.com/zh-CN/support/sections/${section}`;
    let objects = [];
    try {
      const data = await fetchJson("https://www.bitget.com/v1/cms/helpCenter/content/section/helpContentDetail", {
        method: "POST",
        headers: { "content-type": "application/json", locale: "zh-CN", origin: "https://www.bitget.com" },
        body: JSON.stringify({
          pageNum: 1,
          pageSize: 20,
          params: { sectionId: section, firstSearchTime: Date.now(), languageId: 1 }
        })
      }, 8e3, trace);
      if (String(data?.code) !== "200" || !Array.isArray(data?.data?.items)) {
        throw new Error(`Bitget CMS application status ${String(data?.code ?? "unknown")}`);
      }
      objects = data.data.items;
    } catch {
      const html = await fetchText(`${source}?monitor_ts=${Math.floor(Date.now() / 6e4)}`, 8e3, trace);
      for (const id of ["__NEXT_DATA__", "__NUXT_DATA__"]) {
        try {
          objects = walkObjects(scriptJson(html, id));
          break;
        } catch {
        }
      }
      if (!objects.length) {
        try {
          objects = walkObjects(scriptVariableJson(html, "window.__ZEUS_REACT_QUERY_STATE__"));
        } catch {
        }
      }
      if (!objects.length) {
        const titles = [...html.matchAll(/<a[^>]+href=["']([^"']*support\/articles\/[^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)];
        return titles.map((match) => announcementRow("bg", match[2], new URL(match[1], source).toString(), null, fallback, market)).filter(Boolean);
      }
    }
    return objects.map((article) => {
      const href = article.url ?? article.link ?? article.path ?? (article.contentId ? `/zh-CN/support/articles/${article.contentId}` : "");
      return announcementRow(
        "bg",
        article.title,
        href ? new URL(String(href), source).toString() : source,
        article.showTime ?? article.publishTime ?? article.createdAt ?? article.releaseTime,
        fallback,
        market
      );
    }).filter(Boolean);
  }));
  return batches.flat();
}
__name(fetchBitgetNews, "fetchBitgetNews");
async function fetchOkxNews(trace) {
  const sections = [
    ["listing", "spot", "announcements-new-listings"],
    ["delisting", "spot", "announcements-delistings"],
    ["unknown", "unknown", "announcements-trading-updates"],
    ["unknown", "unknown", "announcements-latest-announcements"]
  ];
  const batches = await Promise.all(sections.map(async ([fallback, market, section]) => {
    const source = `https://www.okx.com/en-ar/help/section/${section}`;
    const state = scriptJson(await fetchText(source, 8e3, trace), "appState");
    const list = state?.appContext?.initialProps?.sectionData?.articleList?.list ?? [];
    return list.map((article) => announcementRow(
      "okx",
      article.title ?? "",
      `https://www.okx.com/en-ar/help/${article.slug ?? article.id ?? ""}`,
      article.publishTime,
      fallback,
      market
    )).filter(Boolean);
  }));
  return batches.flat();
}
__name(fetchOkxNews, "fetchOkxNews");
async function fetchGateNews(trace) {
  const source = "https://www.gate.com/zh/announcements/newlisted";
  try {
    const state = scriptJson(await fetchText(source, 3e3, trace), "__NEXT_DATA__");
    const articles = state?.props?.pageProps?.listData?.list;
    if (!Array.isArray(articles) || !articles.length) throw new Error("\u5B98\u65B9\u4E0A\u65B0\u76EE\u5F55\u4E3A\u7A7A");
    return articles.map((article) => {
      const market = Number(article.cate_id) === 37 ? "contract" : Number(article.cate_id) === 38 ? "spot" : "unknown";
      return announcementRow(
        "gate",
        article.title ?? "",
        new URL(article.url, source).href,
        article.release_timestamp ?? article.created_t,
        "listing",
        market
      );
    }).filter((row) => Boolean(row));
  } catch {
    const markdown = await fetchText(`https://r.jina.ai/http://www.gate.com/zh/announcements/newlisted`, 8e3, trace);
    const rows = /* @__PURE__ */ new Map();
    for (const match of markdown.matchAll(/\[([^\]]+)\]\((https?:\/\/www\.gate\.com\/zh\/announcements\/article\/\d+)\)/g)) {
      const title = match[1].replace(/\s+(?:\d+\s*(?:小时|天|月|年)前|20\d{2}-\d{2}-\d{2})\s+[\d,]+$/, "");
      const url = match[2].replace(/^http:/, "https:");
      const row = announcementRow("gate", title, url, null, "listing");
      if (row) rows.set(url, row);
    }
    if (!rows.size) throw new Error("Gate \u5B98\u65B9\u4E0A\u65B0\u76EE\u5F55\u672A\u63D0\u53D6\u5230\u516C\u544A\uFF0C\u4E0D\u80FD\u5F53\u4F5C\u8BFB\u53D6\u6210\u529F");
    return [...rows.values()];
  }
}
__name(fetchGateNews, "fetchGateNews");
function articleBodyText(html) {
  return html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, "").replace(/<br\s*\/?\s*>/gi, "\n").replace(/<\/(?:td|th)>/gi, " | ").replace(/<\/(?:p|div|li|tr|h[1-6])>/gi, "\n").replace(/<[^>]+>/g, " ").replace(/&nbsp;|&#160;/g, " ").replace(/&amp;/g, "&");
}
__name(articleBodyText, "articleBodyText");
function normalizeListingDates(text) {
  const months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  const month = "(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\\.?";
  return text.replace(
    new RegExp("(\\d{1,2})\\s+" + month + "\\s*,?\\s*(20\\d{2})", "gi"),
    (_, d, m, y) => `${y}-${months.indexOf(m.toLowerCase().slice(0, 3)) + 1}-${d}`
  ).replace(
    new RegExp(month + "\\s+(\\d{1,2}),?\\s*(20\\d{2})", "gi"),
    (_, m, d, y) => `${y}-${months.indexOf(m.toLowerCase().slice(0, 3)) + 1}-${d}`
  ).replace(/(\d{1,2}:\d{2}(?::\d{2})?)\s*(\(?(?:UTC|GMT)(?:\s*[+-]\s*\d{1,2}(?::\d{2})?)?\)?)\s*(?:on\s+|,\s*)(20\d{2}-\d{1,2}-\d{1,2})/gi, "$3 $1 $2");
}
__name(normalizeListingDates, "normalizeListingDates");
function scopedAnnouncementText(base, body) {
  const article = body.match(/<article\b[^>]*>([\s\S]*?)<\/article>/i);
  let text = articleBodyText(article?.[1] ?? body).split(/\n\s*(?:相关文章|相關文章|Related articles|Related Articles)\s*\n/)[0];
  const title = text.lastIndexOf(base.title, Math.min(text.length, 6e3));
  if (title >= 0) text = text.slice(title + base.title.length);
  return text.slice(0, 8e4);
}
__name(scopedAnnouncementText, "scopedAnnouncementText");
function binanceArticleText(value) {
  if (typeof value === "string") {
    try {
      return binanceArticleText(JSON.parse(value));
    } catch {
      return articleBodyText(value);
    }
  }
  if (Array.isArray(value)) return value.map(binanceArticleText).join("");
  if (!value || typeof value !== "object") return "";
  const row = value;
  if (row.node === "text") return row.text ?? "";
  if (["p", "li", "td", "th"].includes(row.tag ?? "")) {
    const text = collectArticleText(value).join("").trim();
    return text + (row.tag === "td" || row.tag === "th" ? " | " : "\n\n");
  }
  return (row.child ?? []).map(binanceArticleText).join("") + (row.tag === "tr" || row.tag === "table" ? "\n\n" : "");
}
__name(binanceArticleText, "binanceArticleText");
function listingDateMatches(text) {
  return [...text.matchAll(/(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*[,，T]?\s*(?:at\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?/g)];
}
__name(listingDateMatches, "listingDateMatches");
function listingDateIso(match, context) {
  const zone = context.match(/(?:UTC|GMT)\s*([+-]\s*\d{1,2})(?::(\d{2}))?/i);
  const offset = zone ? Number(zone[1].replace(/\s/g, "")) + Number(zone[2] ?? 0) / 60 * (zone[1].includes("-") ? -1 : 1) : /UTC|GMT|世界标准时间|协调世界时/i.test(context) ? 0 : /北京|东八区|東八區|香港|新加坡|[\u4e00-\u9fff]/.test(context) ? 8 : null;
  if (offset === null) return null;
  const [, y, m, d, h, minute, second] = match;
  const check = new Date(Date.UTC(+y, +m - 1, +d, +h, +minute, +(second ?? 0)));
  if (check.getUTCFullYear() !== +y || check.getUTCMonth() !== +m - 1 || check.getUTCDate() !== +d || +h > 23 || +minute > 59 || +(second ?? 0) > 59) return null;
  return new Date(check.valueOf() - offset * 36e5).toISOString();
}
__name(listingDateIso, "listingDateIso");
function listingRowsFromText(base, body) {
  if (base.action === "delisting") return delistingRowsFromText(base, body);
  const normalized = normalizeListingDates(scopedAnnouncementText(base, body));
  const symbols = new Set(base.symbols ?? (base.symbol ? [base.symbol] : []));
  for (const match of base.title.matchAll(/[（(]([A-Z0-9]{1,24})[）)]/g)) symbols.add(match[1]);
  for (const match of normalized.matchAll(/([\u4e00-\u9fff]{1,16})\/USDT/g)) symbols.add(match[1]);
  for (const match of normalized.matchAll(/(?:^|[^A-Z0-9])([A-Z0-9]{1,24})(?:[/_-])?USDT(?:[^A-Z0-9]|$)/g)) {
    if (!SYMBOL_EXCLUDE.has(match[1])) symbols.add(match[1]);
  }
  for (const alias of projectNameAliases(base.title)) symbols.delete(alias);
  const plans = /* @__PURE__ */ new Map();
  const remember = /* @__PURE__ */ __name((symbol, time) => {
    if (!time) return;
    const times = plans.get(symbol) ?? /* @__PURE__ */ new Set();
    times.add(time);
    plans.set(symbol, times);
  }, "remember");
  const inText = /* @__PURE__ */ __name((text) => [...symbols].filter((symbol) => titleHasSymbol(text, symbol) || symbol.endsWith("USD") && titleHasSymbol(text.replace(/\s+USD\b/g, "USD"), symbol)), "inText");
  const lines = normalized.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  let columns = [];
  let previous = "";
  for (const line of lines) {
    const cells = line.split("|").map((cell) => cell.trim());
    if (cells.length > 2) {
      if (cells.some((cell) => /USDT/.test(cell)) && !listingDateMatches(line).length) {
        const candidate = cells.map(inText);
        if (candidate.some((cell) => cell.length)) columns = candidate;
      }
      if (/上线时间|上線時間|launch\s+(?:time|date)|trading\s+(?:starts|time)/i.test(cells[0])) {
        cells.forEach((cell, index) => {
          const dates2 = listingDateMatches(cell);
          if (dates2.length === 1) for (const symbol of columns[index] ?? []) remember(symbol, listingDateIso(dates2[0], cell + " " + cells[0]));
        });
      }
      previous = line;
      continue;
    }
    const dates = listingDateMatches(line);
    if (/提现|提币|提幣|withdraw|充值|充幣|deposit|活动期间|活動期間|闪兑交易开始|閃兌交易開始|CandyDrop.*(?:时间|時間)|convert trading.*(?:start|open)/i.test(line)) {
      previous = line;
      continue;
    }
    let lineSymbols = inText(line);
    if (!lineSymbols.length && symbols.size === 1 && /(?:现货|現貨)?交易(?:开放|開放|开始|開始|开盘|開盤)时间|(?:spot )?trading (?:opens|starts|start time)/i.test(line)) lineSymbols = [...symbols];
    const opening = /将于|將於|将在|將在|定于|定於|上线时间|上線時間|交易(?:开放|開放|开始|開始)?时间|交易時間|开盘时间|開盤時間|开始交易|開始交易|will\s+(?:list|launch|open)|trading\s+(?:will\s+)?(?:start|open|available)|go\s+live/i.test(line);
    const underListingHeading = /以下时间.*上线|以下時間.*上線|上线时间|上線時間|as follows|following.*(?:schedule|time)/i.test(previous);
    const depositOnly = /充值|充幣|deposit/i.test(line) && !/上线|上線|开始交易|開始交易|will\s+(?:list|launch)|trading\s+(?:starts|opens)/i.test(line);
    if (dates.length === 1 && lineSymbols.length && (opening || underListingHeading || // Date-first list items carry their own timezone and perpetual label.
    /^\s*(?:[-*•]\s*)?20\d{2}/.test(line) && /合约|合約|perpetual/i.test(line))) {
      if (!depositOnly) for (const symbol of lineSymbols) remember(symbol, listingDateIso(dates[0], line));
    }
    previous = line;
  }
  const rows = [...symbols].map((symbol) => {
    const matchedTimes = [...plans.get(symbol) ?? []];
    const scheduledAt = matchedTimes.length === 1 ? matchedTimes[0] : matchedTimes.length > 1 ? null : base.scheduledAt;
    const row = {
      ...base,
      symbol,
      symbols: [symbol],
      scheduledAt,
      detailText: normalized,
      detailParserVersion: 3,
      detailCheckedAt: (/* @__PURE__ */ new Date()).toISOString(),
      detailStatus: scheduledAt || normalized.split(/\n+/).some((line) => (titleHasSymbol(line, symbol) || symbols.size === 1) && /已上线|已上線|现已开放交易|現已開放交易|已于.{0,60}上线|已於.{0,60}上線|now live|now available|now supports/i.test(line)) ? "parsed" : "ambiguous",
      detailError: null
    };
    row.key = announcementIdentityKey(row);
    return row;
  });
  return rows.length ? rows : [{ ...base, detailParserVersion: 3, detailText: normalized, detailCheckedAt: (/* @__PURE__ */ new Date()).toISOString(), detailStatus: "ambiguous" }];
}
__name(listingRowsFromText, "listingRowsFromText");
function delistingRowsFromText(base, body) {
  const text = normalizeListingDates(scopedAnnouncementText(base, body));
  const symbols = [.../* @__PURE__ */ new Set([...base.symbols ?? [], ...base.symbol ? [base.symbol] : [], ...extractSymbols(text)])];
  const plans = /* @__PURE__ */ new Map();
  for (const symbol of symbols) plans.set(symbol, { removal: /* @__PURE__ */ new Set(), closeOnly: /* @__PURE__ */ new Set() });
  const lines = text.split(/\n+/).map((value) => value.trim()).filter(Boolean);
  for (const [index, line] of lines.entries()) {
    const dates = listingDateMatches(line);
    if (dates.length !== 1) continue;
    const closing = /(?:暂停|停止|禁止|suspend|stop|disable).*?(?:开新仓|开仓|新开仓|open(?:ing)?\s+(?:new\s+)?positions)|close[- ]only/i.test(line);
    const removal = /下架|移除|停止.*交易|终止.*交易|delist|remov|cease.*trading|terminat.*trading/i.test(line);
    if (!closing && !removal) continue;
    const time = listingDateIso(dates[0], line);
    if (!time) continue;
    const removalIndex = line.search(/下架|移除|停止.*交易|终止.*交易|delist|remov|cease.*trading|terminat.*trading/i);
    const subject = removalIndex >= 0 ? line.slice(0, removalIndex) : line;
    if (!closing && /充值|提现|提币|deposit|withdraw|BOT|机器人|策略|跟单|杠杆|理财|借贷|闪兑|兑换|margin|earn|loan|convert|copy.trading/i.test(subject)) continue;
    let affected = symbols.filter((value) => titleHasSymbol(line, value));
    if (!affected.length && /(?:下架|移除).*(?:币对|交易对|合约|幣對|交易對|合約).*[：:]\s*$/.test(line)) {
      const next = lines[index + 1] ?? "";
      if (!listingDateMatches(next).length) affected = symbols.filter((value) => titleHasSymbol(next, value));
    }
    for (const symbol of affected) {
      plans.get(symbol)[closing ? "closeOnly" : "removal"].add(time);
    }
  }
  const only = /* @__PURE__ */ __name((times) => times.size === 1 ? [...times][0] : null, "only");
  const rows = symbols.map((symbol) => {
    const plan = plans.get(symbol);
    const scheduledAt = only(plan.removal);
    const row = {
      ...base,
      symbol,
      symbols: [symbol],
      scheduledAt,
      openingSuspendsAt: only(plan.closeOnly),
      detailText: text,
      detailParserVersion: 2,
      detailCheckedAt: (/* @__PURE__ */ new Date()).toISOString(),
      detailStatus: scheduledAt ? "parsed" : "ambiguous",
      detailError: null
    };
    row.key = announcementIdentityKey(row);
    return row;
  });
  return rows.length ? rows : [{ ...base, scheduledAt: null, detailText: text, detailParserVersion: 2, detailCheckedAt: (/* @__PURE__ */ new Date()).toISOString(), detailStatus: "ambiguous" }];
}
__name(delistingRowsFromText, "delistingRowsFromText");
function parseBitgetDetail(base, html) {
  const id = new URL(base.url).pathname.match(/\/support\/articles\/(\d+)$/)?.[1];
  const state = scriptVariableJson(html, "window.__ZEUS_REACT_QUERY_STATE__");
  const article = walkObjects(state).find((row2) => String(row2.contentId) === id && typeof row2.content === "string" && typeof row2.title === "string");
  if (!id || !article || article.content.length < 80 || article.content.length > 5e5) throw new Error("Bitget \u516C\u544A\u6B63\u6587\u8EAB\u4EFD\u6216\u683C\u5F0F\u4E0D\u5339\u914D");
  const row = {
    ...base,
    title: article.title,
    action: classifyAction(article.title, base.action),
    marketType: classifyMarket(article.title, base.marketType),
    publishedAt: asIso(article.showTime) ?? base.publishedAt,
    symbols: extractSymbols(article.title),
    detailSource: "official_page"
  };
  return listingRowsFromText(row, article.content);
}
__name(parseBitgetDetail, "parseBitgetDetail");
function parseBinanceDetail(base, article, checkedAt = (/* @__PURE__ */ new Date()).toISOString()) {
  if (!/^[a-f0-9]{32}$/i.test(article.code) || !base.url.includes(article.code) || typeof article.title !== "string" || typeof article.body !== "string" || article.body.length > 5e5) {
    throw new Error("\u516C\u544A\u6B63\u6587\u8EAB\u4EFD\u6216\u683C\u5F0F\u4E0D\u5339\u914D");
  }
  const text = binanceArticleText(article.body);
  if (text.length < 80) throw new Error("\u516C\u544A\u6B63\u6587\u6682\u4E0D\u53EF\u7528");
  return listingRowsFromText({
    ...base,
    title: article.title,
    publishedAt: asIso(article.publishDate),
    detailSource: "official_api",
    assetType: mergedAssetType(base.assetType, assetTypeFromEvidence(text, []))
  }, text).map((row) => ({ ...row, detailCheckedAt: checkedAt }));
}
__name(parseBinanceDetail, "parseBinanceDetail");
async function fetchAnnouncementDetail(base, trace) {
  if (base.exchange === "bnus") return parseBinanceUsDetail(base, await fetchText(base.url, 6e3, trace));
  let text;
  if (base.exchange === "aster") {
    const id = new URL(base.url).pathname.match(/\/announcement\/(\d+)/)?.[1];
    if (!id) throw new Error("Aster \u516C\u544A\u7F16\u53F7\u7F3A\u5931");
    const data = await fetchJson(`https://www.asterdex.com/bapi/composite/v1/public/composite/ae/announcement/get?id=${id}`, { headers: { lang: "en" } }, 4e3, trace);
    const article = data?.data;
    if (String(article?.id) !== id || typeof article?.content !== "string" || !article.content.trim()) throw new Error("Aster \u516C\u544A\u6B63\u6587\u8EAB\u4EFD\u6216\u5185\u5BB9\u4E0D\u5B8C\u6574");
    const rows = listingRowsFromText({ ...base, title: article.title, publishedAt: asIso(article.publishTime) ?? base.publishedAt, detailSource: "official_api" }, article.content);
    if (article.category === "NEW_LISTING" && /^New Perp Listings?:/i.test(article.title) && /^Listing does not constitute an endorsement or investment recommendation\. DYOR\.$/i.test(articleBodyText(article.content).trim())) {
      return rows.map((row) => ({ ...row, scheduledAt: null, detailStatus: row.symbol ? "parsed" : "ambiguous", detailNote: "\u5B98\u65B9\u516C\u544A\u672A\u63D0\u4F9B\u5177\u4F53\u4E0A\u7EBF\u65F6\u95F4\uFF1B\u5B9E\u9645\u53EF\u4EA4\u6613\u65F6\u95F4\u4ECD\u7531\u76D8\u53E3\u786E\u8BA4" }));
    }
    return rows;
  }
  if (base.exchange === "bn") {
    const code = base.url.match(/[a-f0-9]{32}/i)?.[0];
    if (!code) return [base];
    const query = new URLSearchParams({ articleCode: code });
    const detailUrl = `https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?${query}`;
    const data = await fetchJson(detailUrl, { headers: { lang: "zh-CN" } }, 3e3, trace);
    return parseBinanceDetail(base, data?.data);
  } else {
    text = await fetchText(base.url, 4e3, trace);
  }
  if (!text || text.length < 80 || /Access Denied|Just a moment|Security Verification/i.test(text.slice(0, 700))) throw new Error("\u516C\u544A\u6B63\u6587\u6682\u4E0D\u53EF\u7528");
  if (base.exchange === "bg") return parseBitgetDetail(base, text);
  let publishedAt = base.publishedAt;
  if (base.exchange === "gate") {
    const publication = text.match(/(20\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s*\(UTC\+8\)/);
    if (publication) publishedAt = new Date(Date.UTC(+publication[1], +publication[2] - 1, +publication[3], +publication[4] - 8, +publication[5])).toISOString();
  }
  return listingRowsFromText({ ...base, publishedAt }, text);
}
__name(fetchAnnouncementDetail, "fetchAnnouncementDetail");
async function fetchTradingViewContracts(exchange) {
  const venue = exchange === "bn" ? "BINANCE" : "BITGET";
  const data = await fetchJson("https://scanner.tradingview.com/crypto/scan", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      filter: [
        { left: "exchange", operation: "equal", right: venue },
        { left: "type", operation: "equal", right: "swap" },
        { left: "currency", operation: "equal", right: "USDT" }
      ],
      options: { lang: "en" },
      symbols: { query: { types: [] }, tickers: [] },
      columns: ["name", "base_currency"]
    })
  }, 8e3);
  return (data?.data ?? []).map((row) => {
    const marketSymbol = String(row?.d?.[0] ?? "").replace(/\.P$/i, "").toUpperCase();
    const symbol = String(row?.d?.[1] ?? marketSymbol.replace(/USDT$/i, "")).toUpperCase();
    return { symbol, marketSymbol, scheduledAt: null, marketStatus: null, assetType: "unknown" };
  }).filter((row) => row.symbol && row.marketSymbol.endsWith("USDT"));
}
__name(fetchTradingViewContracts, "fetchTradingViewContracts");
function binanceUsCatalogue(xml) {
  if (!xml.includes("<urlset")) throw new Error("Binance.US \u7AD9\u70B9\u5730\u56FE\u683C\u5F0F\u5F02\u5E38");
  const rows = [];
  for (const match of xml.matchAll(/<url>\s*([\s\S]*?)<\/url>/g)) {
    const url = match[1].match(/<loc>(.*?)<\/loc>/)?.[1];
    if (!url || !/^https:\/\/support\.binance\.us\/en\/articles\/\d+-binance-us-(?:lists-|will-delist-)/.test(url)) continue;
    const title = url.split("/").at(-1).replace(/^\d+-/, "").replace(/-/g, " ");
    const row = announcementRow("bnus", title, url, null, /will-delist-/.test(url) ? "delisting" : "listing");
    if (!row) continue;
    rows.push({ ...row, marketType: "spot", sourceUpdatedAt: asIso(match[1].match(/<lastmod>(.*?)<\/lastmod>/)?.[1]) });
  }
  if (!rows.length) throw new Error("Binance.US \u7AD9\u70B9\u5730\u56FE\u672A\u8BC6\u522B\u5230\u4E0A\u67B6\u516C\u544A");
  return rows.sort((a, b) => Date.parse(b.sourceUpdatedAt ?? "") - Date.parse(a.sourceUpdatedAt ?? "")).slice(0, 20);
}
__name(binanceUsCatalogue, "binanceUsCatalogue");
async function fetchBinanceUsNews(trace) {
  return binanceUsCatalogue(await fetchText("https://support.binance.us/sitemap.xml", 6e3, trace));
}
__name(fetchBinanceUsNews, "fetchBinanceUsNews");
function binanceUsEasternTime(line, reference) {
  const months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
  const m = line.match(/(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,?\s+(20\d{2}))?\s+at\s+(\d{1,2}):(\d{2})\s*(AM|PM)\s+(ET|EST|EDT)\b/i);
  const referenceMs = Date.parse(reference ?? "");
  if (!m || !Number.isFinite(referenceMs) || +m[4] < 1 || +m[4] > 12 || +m[5] > 59) return null;
  const month = months.indexOf(m[1].toLowerCase()), day = +m[2], hour = +m[4] % 12 + (m[6].toUpperCase() === "PM" ? 12 : 0), minute = +m[5];
  const referenceYear = new Date(referenceMs).getUTCFullYear();
  const years = m[3] ? [+m[3]] : [referenceYear - 1, referenceYear, referenceYear + 1];
  const fmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const candidates = [];
  for (const year of years) for (const offset of [4, 5]) {
    if (m[7].toUpperCase() === "EST" && offset !== 5 || m[7].toUpperCase() === "EDT" && offset !== 4) continue;
    const utc = Date.UTC(year, month, day, hour + offset, minute);
    const p = Object.fromEntries(fmt.formatToParts(utc).map((p2) => [p2.type, p2.value]));
    if (+p.year === year && +p.month === month + 1 && +p.day === day && +p.hour === hour && +p.minute === minute && Math.abs(utc - referenceMs) < 45 * 864e5) candidates.push(utc);
  }
  return candidates.length === 1 ? new Date(candidates[0]).toISOString() : null;
}
__name(binanceUsEasternTime, "binanceUsEasternTime");
function parseBinanceUsDetail(base, html) {
  const article = scriptJson(html, "__NEXT_DATA__")?.props?.pageProps?.articleContent;
  const id = new URL(base.url).pathname.match(/\/articles\/(\d+)/)?.[1];
  if (!article || String(article.articleId) !== id || typeof article.title !== "string" || !Array.isArray(article.blocks)) throw new Error("Binance.US \u516C\u544A\u6B63\u6587\u8EAB\u4EFD\u4E0D\u5339\u914D");
  const text = article.blocks.filter((b) => typeof b.text === "string").map((b) => articleBodyText(b.text)).join("\n");
  const reference = asIso(article.lastUpdatedDate);
  const pairs = [...text.matchAll(/\b([A-Z0-9]{1,24})\/USDT\b/g)].map((m) => m[1]);
  const symbols = [...new Set(pairs.length ? pairs : extractSymbols(article.title.replace(/\blists\b/i, "list")))];
  const action = classifyAction(article.title, base.action);
  return symbols.map((symbol) => {
    const times = new Set(text.split("\n").filter((line) => titleHasSymbol(line, symbol) && /trading.*\bbegins\b/i.test(line)).map((line) => binanceUsEasternTime(line, reference)).filter((v) => Boolean(v)));
    const scheduledAt = times.size === 1 ? [...times][0] : null;
    const row = {
      ...base,
      title: article.title,
      symbol,
      symbols: [symbol],
      marketType: "spot",
      action,
      scheduledAt,
      publishedAt: null,
      sourceUpdatedAt: reference,
      detailText: text,
      detailStatus: scheduledAt || /\|\s*Trade now/i.test(article.title) ? "parsed" : "ambiguous",
      detailSource: "official_page",
      detailCheckedAt: (/* @__PURE__ */ new Date()).toISOString(),
      detailParserVersion: 3,
      detailNote: "\u6765\u6E90\u53EA\u63D0\u4F9B\u66F4\u65B0\u65F6\u95F4\uFF1B\u4E0D\u5C06\u66F4\u65B0\u65F6\u95F4\u4F5C\u4E3A\u65B0\u95FB\u53D1\u5E03\u65F6\u95F4"
    };
    row.key = announcementIdentityKey(row);
    return row;
  });
}
__name(parseBinanceUsDetail, "parseBinanceUsDetail");
var NEWS_FETCHERS = {
  bn: fetchBinanceNews,
  bnus: fetchBinanceUsNews,
  bg: fetchBitgetNews,
  by: fetchBybitNews,
  gate: fetchGateNews,
  okx: fetchOkxNews,
  aster: fetchAsterNews
};
async function fetchContracts(exchange) {
  if (exchange === "bn" || exchange === "aster") {
    const urls = exchange === "bn" ? ["https://fapi.binance.com/fapi/v1/exchangeInfo", "https://www.binance.com/fapi/v1/exchangeInfo"] : ["https://fapi.asterdex.com/fapi/v1/exchangeInfo"];
    let symbols;
    try {
      const data2 = await fetchJsonFallback(urls);
      symbols = data2?.symbols ?? [];
    } catch (error) {
      if (exchange !== "bn") throw error;
      try {
        const stream = await collectWebSocketJsonFallback([
          "wss://fstream.binance.com/market/ws/!markPrice@arr@1s",
          "wss://fstream.binance.com/market/stream?streams=!markPrice@arr@1s"
        ], 1250, 5e3, 2);
        symbols = Array.from(new Map(stream.map((row) => [String(row.s ?? ""), {
          symbol: row.s,
          baseAsset: String(row.s ?? "").replace(/USDT$/i, ""),
          quoteAsset: "USDT",
          contractType: "PERPETUAL",
          status: "TRADING",
          st: row.st,
          incomplete: true
        }])).values());
      } catch {
        return (await fetchTradingViewContracts("bn")).map((row) => ({ ...row, coverage: "partial", dataSource: "TradingView" }));
      }
    }
    return symbols.filter(
      (row) => String(row.quoteAsset ?? "").toUpperCase() === "USDT" && (!row.contractType || String(row.contractType).toUpperCase().endsWith("PERPETUAL")) && Number(row.st ?? 1) === 1 && !["SETTLING", "CLOSE"].includes(String(row.status ?? "").toUpperCase())
    ).map((row) => ({
      symbol: String(row.baseAsset ?? "").toUpperCase(),
      marketSymbol: String(row.symbol ?? "").toUpperCase(),
      scheduledAt: asIso(row.onboardDate),
      marketStatus: String(row.status ?? "") || null,
      assetType: assetTypeFromMetadata(row, String(row.baseAsset ?? "").toUpperCase()),
      coverage: row.incomplete ? "partial" : "complete",
      dataSource: row.incomplete ? "official_mark_stream" : "official_rest",
      contractKind: "perpetual"
    }));
  }
  if (exchange === "by") {
    const rows = [];
    let cursor = "";
    for (let page = 0; page < 6; page += 1) {
      const query = new URLSearchParams({ category: "linear", limit: "1000" });
      if (cursor) query.set("cursor", cursor);
      const path = `/v5/market/instruments-info?${query}`;
      const data2 = await fetchJsonFallback([`https://api.bytick.com${path}`, `https://api.bybit.com${path}`]);
      rows.push(...data2?.result?.list ?? []);
      cursor = data2?.result?.nextPageCursor ?? "";
      if (!cursor) break;
    }
    return rows.filter((row) => String(row.quoteCoin ?? "").toUpperCase() === "USDT" && String(row.settleCoin ?? "USDT").toUpperCase() === "USDT").map((row) => ({
      symbol: String(row.baseCoin ?? "").toUpperCase(),
      marketSymbol: String(row.symbol ?? "").toUpperCase(),
      scheduledAt: asIso(row.launchTime),
      marketStatus: String(row.status ?? "") || null,
      assetType: assetTypeFromMetadata(row, String(row.baseCoin ?? "").toUpperCase()),
      contractKind: row.contractType === "LinearFutures" ? "delivery" : "perpetual"
    }));
  }
  if (exchange === "bg") {
    let data2;
    try {
      data2 = await fetchJsonFallback([
        "https://api.bitget.com/api/v3/market/instruments?category=USDT-FUTURES",
        "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES"
      ], { headers: { origin: "https://www.bitget.com", referer: "https://www.bitget.com/", locale: "en-US" } });
    } catch (error) {
      throw new Error(`Bitget \u5B98\u65B9\u5408\u7EA6\u5217\u8868\u8BFB\u53D6\u5931\u8D25\uFF0C\u672A\u4F7F\u7528\u4E0D\u5B8C\u6574\u7B2C\u4E09\u65B9\u5217\u8868\uFF1A${String(error)}`);
    }
    return (data2?.data ?? []).filter((row) => {
      const status = String(row.symbolStatus ?? row.status ?? "normal").toLowerCase();
      return String(row.quoteCoin ?? "USDT").toUpperCase() === "USDT" && !["off", "offline"].includes(status);
    }).map((row) => ({
      symbol: String(row.baseCoin ?? String(row.symbol ?? "").replace(/USDT$/i, "")).toUpperCase(),
      marketSymbol: String(row.symbol ?? "").toUpperCase(),
      scheduledAt: asIso(row.launchTime || row.openTime || row.deliveryStartTime),
      marketStatus: String(row.symbolStatus ?? row.status ?? "") || null,
      assetType: assetTypeFromMetadata(row, String(row.baseCoin ?? String(row.symbol ?? "").replace(/USDT$/i, "")).toUpperCase()),
      dataSource: "official_rest",
      coverage: "complete",
      contractKind: "perpetual"
    }));
  }
  if (exchange === "gate") {
    const data2 = await fetchJson("https://api.gateio.ws/api/v4/futures/usdt/contracts");
    return (Array.isArray(data2) ? data2 : []).filter((row) => !row.in_delisting && String(row.name ?? "").endsWith("_USDT")).map((row) => ({
      symbol: String(row.name).slice(0, -5).toUpperCase(),
      marketSymbol: String(row.name).toUpperCase(),
      scheduledAt: asIso(row.launch_time || row.create_time),
      marketStatus: String(row.status ?? "") || (row.in_delisting ? "delisting" : null),
      assetType: assetTypeFromMetadata(row, String(row.name).slice(0, -5).toUpperCase())
    }));
  }
  if (exchange === "okx") {
    const path = "/api/v5/public/instruments?instType=SWAP";
    let rows;
    try {
      const data2 = await fetchJsonFallback([`https://openapi.okx.com${path}`, `https://www.okx.com${path}`]);
      rows = data2?.data ?? [];
    } catch {
      rows = await requestWebSocketSnapshot([
        "wss://ws.okx.com:8443/ws/v5/public",
        "wss://wsaws.okx.com:8443/ws/v5/public"
      ], { op: "subscribe", args: [{ channel: "instruments", instType: "SWAP" }] });
    }
    return rows.filter((row) => String(row.instId ?? "").endsWith("-USDT-SWAP") && ["live", "preopen"].includes(String(row.state ?? "").toLowerCase())).map((row) => ({
      symbol: String(row.instId).slice(0, -10).replace(/-/g, "").toUpperCase(),
      marketSymbol: String(row.instId).toUpperCase(),
      scheduledAt: asIso(row.listTime),
      marketStatus: String(row.state ?? "") || null,
      assetType: assetTypeFromMetadata(row, String(row.instId).slice(0, -10).replace(/-/g, "").toUpperCase())
    }));
  }
  const data = await fetchJson("https://api.hyperliquid.xyz/info", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ type: "meta" })
  });
  return (data?.universe ?? []).map((row) => ({
    symbol: String(row.name ?? "").toUpperCase(),
    marketSymbol: String(row.name ?? "").toUpperCase(),
    scheduledAt: null,
    marketStatus: null,
    assetType: assetTypeFromMetadata(row, String(row.name ?? "").toUpperCase())
  }));
}
__name(fetchContracts, "fetchContracts");
async function fetchBook(exchange, marketSymbol) {
  let bid = null;
  let ask = null;
  if (exchange === "bn" || exchange === "aster") {
    const path = `/fapi/v1/ticker/bookTicker?symbol=${encodeURIComponent(marketSymbol)}`;
    const urls = exchange === "bn" ? [`https://fapi.binance.com${path}`, `https://www.binance.com${path}`] : [`https://fapi.asterdex.com${path}`];
    let data;
    try {
      data = await fetchJsonFallback(urls, {}, 4e3);
    } catch (error) {
      if (exchange !== "bn") throw error;
      try {
        data = (await collectWebSocketJsonFallback([
          `wss://fstream.binance.com/public/ws/${marketSymbol.toLowerCase()}@bookTicker`,
          `wss://fstream.binance.com/public/stream?streams=${marketSymbol.toLowerCase()}@bookTicker`
        ], 0, 4e3, 2))[0];
      } catch (streamError) {
        throw new Error("Binance \u5B98\u65B9\u76D8\u53E3\u4E0D\u53EF\u7528\uFF1A" + String(error) + "\uFF1B" + String(streamError));
      }
    }
    bid = asNumber(data?.bidPrice ?? data?.b);
    ask = asNumber(data?.askPrice ?? data?.a);
  } else if (exchange === "by") {
    const path = `/v5/market/tickers?category=linear&symbol=${encodeURIComponent(marketSymbol)}`;
    const data = await fetchJsonFallback([`https://api.bytick.com${path}`, `https://api.bybit.com${path}`], {}, 4e3);
    const row = data?.result?.list?.[0];
    bid = asNumber(row?.bid1Price);
    ask = asNumber(row?.ask1Price);
  } else if (exchange === "bg") {
    try {
      const query = new URLSearchParams({ symbol: marketSymbol, productType: "USDT-FUTURES", limit: "1" });
      const data = await fetchJson(`https://api.bitget.com/api/v2/mix/market/orderbook?${query}`, {
        headers: { origin: "https://www.bitget.com", referer: "https://www.bitget.com/", locale: "en-US" }
      }, 4e3);
      bid = asNumber(data?.data?.bids?.[0]?.[0]);
      ask = asNumber(data?.data?.asks?.[0]?.[0]);
    } catch (error) {
      throw new Error(`Bitget \u5B98\u65B9\u76D8\u53E3\u6682\u4E0D\u53EF\u7528\uFF1A${String(error)}`);
    }
  } else if (exchange === "gate") {
    const query = new URLSearchParams({ contract: marketSymbol, limit: "1" });
    const data = await fetchJson(`https://api.gateio.ws/api/v4/futures/usdt/order_book?${query}`, {}, 4e3);
    bid = asNumber(data?.bids?.[0]?.p ?? data?.bids?.[0]?.[0]);
    ask = asNumber(data?.asks?.[0]?.p ?? data?.asks?.[0]?.[0]);
  } else if (exchange === "okx") {
    const path = `/api/v5/market/books?instId=${encodeURIComponent(marketSymbol)}&sz=1`;
    let row;
    try {
      const data = await fetchJsonFallback([`https://openapi.okx.com${path}`, `https://www.okx.com${path}`], {}, 4e3);
      row = data?.data?.[0];
    } catch {
      row = (await requestWebSocketSnapshot([
        "wss://ws.okx.com:8443/ws/v5/public",
        "wss://wsaws.okx.com:8443/ws/v5/public"
      ], { op: "subscribe", args: [{ channel: "bbo-tbt", instId: marketSymbol }] }, 4e3))[0];
    }
    bid = asNumber(row?.bids?.[0]?.[0]);
    ask = asNumber(row?.asks?.[0]?.[0]);
  } else {
    const data = await fetchJson("https://api.hyperliquid.xyz/info", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ type: "l2Book", coin: marketSymbol })
    }, 4e3);
    bid = asNumber(data?.levels?.[0]?.[0]?.px);
    ask = asNumber(data?.levels?.[1]?.[0]?.px);
  }
  return bid && ask && ask >= bid ? { bid, ask } : null;
}
__name(fetchBook, "fetchBook");
async function fetchSpotBook(exchange, marketSymbol) {
  let bid = null;
  let ask = null;
  if (exchange === "bn" || exchange === "bnus") {
    const host = exchange === "bnus" ? "api.binance.us" : "api.binance.com";
    const row = await fetchJson(`https://${host}/api/v3/ticker/bookTicker?symbol=${encodeURIComponent(marketSymbol)}`, {}, 3e3);
    bid = asNumber(row.bidPrice);
    ask = asNumber(row.askPrice);
  } else if (exchange === "by") {
    const row = await fetchJson(`https://api.bybit.com/v5/market/orderbook?category=spot&symbol=${encodeURIComponent(marketSymbol)}&limit=1`, {}, 3e3);
    bid = asNumber(row?.result?.b?.[0]?.[0]);
    ask = asNumber(row?.result?.a?.[0]?.[0]);
  } else if (exchange === "bg") {
    const row = await fetchJson(`https://api.bitget.com/api/v2/spot/market/orderbook?symbol=${encodeURIComponent(marketSymbol)}&type=step0&limit=1`, {}, 3e3);
    bid = asNumber(row?.data?.bids?.[0]?.[0]);
    ask = asNumber(row?.data?.asks?.[0]?.[0]);
  } else if (exchange === "gate") {
    const row = await fetchJson(`https://api.gateio.ws/api/v4/spot/order_book?currency_pair=${encodeURIComponent(marketSymbol)}&limit=1`, {}, 3e3);
    bid = asNumber(row?.bids?.[0]?.[0]);
    ask = asNumber(row?.asks?.[0]?.[0]);
  } else if (exchange === "okx") {
    const row = await fetchJson(`https://www.okx.com/api/v5/market/books?instId=${encodeURIComponent(marketSymbol)}&sz=1`, {}, 3e3);
    bid = asNumber(row?.data?.[0]?.bids?.[0]?.[0]);
    ask = asNumber(row?.data?.[0]?.asks?.[0]?.[0]);
  }
  return bid && ask && ask >= bid ? { bid, ask } : null;
}
__name(fetchSpotBook, "fetchSpotBook");
function withoutRetiredCardFields(event) {
  const { precardStatus, precardCreatedAt, volumeExemptUntil, normalVolumeGateUsdt, ...rest } = event;
  return rest;
}
__name(withoutRetiredCardFields, "withoutRetiredCardFields");
var MONITOR_STATE_KEY = "monitor-state-v2";
async function readDurableState(storage, cache) {
  return readState(storage, cache);
}
__name(readDurableState, "readDurableState");
async function writeDurableState(storage, state, cache) {
  await writeState(storage, state, cache);
}
__name(writeDurableState, "writeDurableState");
function displayBeijing(value, empty = "\u65F6\u95F4\u672A\u77E5") {
  const timestamp = Date.parse(value ?? "");
  return Number.isFinite(timestamp) ? `${new Date(timestamp + 8 * 36e5).toISOString().slice(0, 19).replace("T", " ")}\uFF08\u5317\u4EAC\u65F6\u95F4\uFF09` : empty;
}
__name(displayBeijing, "displayBeijing");
function emptyPersistedState() {
  return {
    version: 1,
    running: false,
    inventory: { bn: {}, bnus: {}, bg: {}, by: {}, gate: {}, okx: {}, aster: {}, hl: {} },
    announcements: [],
    events: [],
    pushLogs: [],
    activityLogs: [],
    newsReadLogs: [],
    newsHourlyStats: []
  };
}
__name(emptyPersistedState, "emptyPersistedState");
function normalizeStoredBinancePublishedAt(publishedAt, fetchedAt) {
  if (!publishedAt) return null;
  const publishedMs = Date.parse(publishedAt);
  const fetchedMs = Date.parse(fetchedAt);
  if (!Number.isFinite(publishedMs) || !Number.isFinite(fetchedMs)) return publishedAt;
  return publishedMs > fetchedMs + 5 * 6e4 ? new Date(publishedMs - 8 * 60 * 6e4).toISOString() : publishedAt;
}
__name(normalizeStoredBinancePublishedAt, "normalizeStoredBinancePublishedAt");
function normalizePersistedAnnouncements(rows) {
  const normalized = /* @__PURE__ */ new Map();
  for (const input of rows) {
    if (input.exchange === "gate" && !/^https:\/\/www\.gate\.com\/zh\/announcements\/article\/\d+$/.test(canonicalAnnouncementUrl("gate", input.url))) continue;
    const row = {
      ...input,
      action: classifyAction(input.title, input.action),
      marketType: classifyMarket(input.title, input.marketType),
      symbol: input.detailCheckedAt ? input.symbol : extractSymbol(input.title),
      symbols: input.detailCheckedAt ? input.symbols : extractSymbols(input.title),
      url: canonicalAnnouncementUrl(input.exchange, input.url),
      publishedAt: input.exchange === "bn" ? normalizeStoredBinancePublishedAt(input.publishedAt, input.fetchedAt) : input.publishedAt
    };
    row.key = announcementIdentityKey(row);
    const previous = normalized.get(row.key);
    if (!previous) {
      normalized.set(row.key, row);
      continue;
    }
    const previousFetched = Date.parse(previous.fetchedAt);
    const rowFetched = Date.parse(row.fetchedAt);
    normalized.set(row.key, {
      ...mergeAnnouncementRows(previous, row),
      fetchedAt: rowFetched < previousFetched ? row.fetchedAt : previous.fetchedAt,
      publishedAt: row.publishedAt ?? previous.publishedAt,
      assetType: mergedAssetType(previous.assetType, row.assetType)
    });
  }
  return Array.from(normalized.values()).sort((a, b) => Date.parse(b.publishedAt ?? b.fetchedAt) - Date.parse(a.publishedAt ?? a.fetchedAt)).slice(0, 500);
}
__name(normalizePersistedAnnouncements, "normalizePersistedAnnouncements");
function mergeAnnouncementRows(previous, incoming) {
  const detailed = Boolean(previous.detailCheckedAt) || Boolean(incoming.detailCheckedAt);
  const preferred = detailed ? Date.parse(incoming.detailCheckedAt ?? "") >= Date.parse(previous.detailCheckedAt ?? "") || !previous.detailCheckedAt ? incoming : previous : /[\u3400-\u9fff]/.test(previous.title) && !/[\u3400-\u9fff]/.test(incoming.title) ? previous : incoming;
  const other = preferred === previous ? incoming : previous;
  const dates = [previous.publishedAt, incoming.publishedAt].filter((value) => Boolean(value)).sort();
  return {
    ...other,
    ...preferred,
    publishedAt: detailed ? preferred.publishedAt : dates[0] ?? null,
    assetType: mergedAssetType(previous.assetType, incoming.assetType)
  };
}
__name(mergeAnnouncementRows, "mergeAnnouncementRows");
function mergeAnnouncementBatch(previous, incoming, observedAt) {
  previous = normalizePersistedAnnouncements(previous);
  const rows = new Map(previous.map((row) => [row.key, row]));
  const firstSeen = /* @__PURE__ */ new Map();
  for (const row of previous) {
    const key = row.exchange + ":" + canonicalAnnouncementUrl(row.exchange, row.url);
    const at = firstSeen.get(key);
    if (!at || row.fetchedAt < at) firstSeen.set(key, row.fetchedAt);
  }
  for (const item of incoming) {
    const row = { ...item, url: canonicalAnnouncementUrl(item.exchange, item.url) };
    row.key = announcementIdentityKey(row);
    const before = rows.get(row.key);
    const fetchedAt = firstSeen.get(row.exchange + ":" + row.url) ?? observedAt;
    rows.set(row.key, { ...before ? mergeAnnouncementRows(before, row) : row, fetchedAt });
  }
  const resolved = new Set([...rows.values()].filter((row) => row.symbol && row.detailCheckedAt).map((row) => row.exchange + ":" + row.url));
  return [...rows.values()].filter((row) => (!row.symbol || !projectNameAliases(row.title).has(row.symbol)) && (row.symbol || !resolved.has(row.exchange + ":" + row.url)));
}
__name(mergeAnnouncementBatch, "mergeAnnouncementBatch");
function deduplicateCatalogue(rows) {
  const merged = /* @__PURE__ */ new Map();
  for (const input of rows) {
    const row = { ...input, url: canonicalAnnouncementUrl(input.exchange, input.url) };
    row.key = announcementIdentityKey(row);
    const previous = merged.get(row.key);
    merged.set(row.key, previous ? mergeAnnouncementRows(previous, row) : row);
  }
  return [...merged.values()];
}
__name(deduplicateCatalogue, "deduplicateCatalogue");
function detailProgressMessage(rows) {
  const groups = /* @__PURE__ */ new Map();
  for (const row of rows) {
    if (row.action !== "delisting" && (row.action !== "listing" || row.marketType === "unknown")) continue;
    groups.set(row.url, [...groups.get(row.url) ?? [], row]);
  }
  let pending = 0, unavailable = 0, ambiguous = 0;
  for (const items of groups.values()) {
    if (items.some((row) => row.detailStatus === "unavailable")) unavailable++;
    else if (items.some((row) => !row.detailStatus || row.detailStatus === "pending")) pending++;
    else if (items.some((row) => row.detailStatus === "ambiguous")) ambiguous++;
  }
  const parts = [
    pending ? `${pending} \u7BC7\u5F85\u6293\u53D6\u6B63\u6587` : "",
    unavailable ? `${unavailable} \u7BC7\u6B63\u6587\u8BFB\u53D6\u5931\u8D25\uFF08\u7B49\u5F85\u91CD\u8BD5\uFF09` : "",
    ambiguous ? `${ambiguous} \u7BC7\u6B63\u6587\u5DF2\u8BFB\u53D6\uFF0C\u6807\u7684\u6216\u8BA1\u5212\u65F6\u95F4\u672A\u80FD\u660E\u786E\u786E\u8BA4` : ""
  ].filter(Boolean);
  return parts.length ? "\u76EE\u5F55\u5DF2\u8BFB\u53D6\uFF1B" + parts.join("\uFF1B") : null;
}
__name(detailProgressMessage, "detailProgressMessage");
function scheduledMonitorCanStart(scheduledAt, nowMs = Date.now()) {
  const scheduledMs = Date.parse(scheduledAt ?? "");
  return !Number.isFinite(scheduledMs) || scheduledMs <= nowMs;
}
__name(scheduledMonitorCanStart, "scheduledMonitorCanStart");
var EXCHANGE_FETCH_LOCATIONS = {
  bn: "apac-ne",
  bnus: "enam",
  bg: "apac-se",
  by: "apac-se",
  gate: "apac-se",
  okx: "eeur",
  aster: "apac-se",
  hl: "apac-se"
};
function exchangeFetchStub(env, exchange) {
  const locationHint = EXCHANGE_FETCH_LOCATIONS[exchange];
  const id = env.MONITOR.idFromName(`exchange-fetch-${exchange}-${locationHint}-v1`);
  return env.MONITOR.get(id, { locationHint });
}
__name(exchangeFetchStub, "exchangeFetchStub");
var ExchangeMonitor = class extends DurableObject {
  static {
    __name(this, "ExchangeMonitor");
  }
  stateWriteCache = /* @__PURE__ */ new Map();
  persistTail = Promise.resolve();
  lastCheckpointAt = 0;
  alarmBusy = false;
  cyclePromise = null;
  newsPromise = null;
  newsDetailCache = /* @__PURE__ */ new Map();
  newsDetailAttempts = /* @__PURE__ */ new Map();
  contractDiagnostics = /* @__PURE__ */ new Map();
  pushInFlight = /* @__PURE__ */ new Set();
  contractCache = /* @__PURE__ */ new Map();
  contractBackoffUntil = /* @__PURE__ */ new Map();
  contractFailureCounts = /* @__PURE__ */ new Map();
  binanceBooks = null;
  binanceBookFlight = null;
  binanceBookRetryAt = 0;
  binanceBookError = null;
  binanceBookFailures = 0;
  state = emptyPersistedState();
  sources = /* @__PURE__ */ new Map();
  runtime = {
    lastCycleAt: null,
    lastContractScanAt: null,
    lastNewsScanAt: null,
    nextAlarmAt: null,
    lastError: null
  };
  constructor(ctx, env) {
    super(ctx, env);
    if (!this.isPrimaryMonitor()) return;
    ctx.blockConcurrencyWhile(async () => {
      const durable = await readDurableState(ctx.storage, this.stateWriteCache);
      const saved = durable ?? await env.STATE_KV.get(MONITOR_STATE_KEY, "json");
      if (saved?.version === 1) {
        const needsActivityLogUpgrade = !Array.isArray(saved.activityLogs);
        const needsNewsLogUpgrade = !Array.isArray(saved.newsReadLogs);
        const needsNewsHourlyUpgrade = !Array.isArray(saved.newsHourlyStats);
        const normalizedAnnouncements = normalizePersistedAnnouncements(saved.announcements ?? []);
        const announcementsChanged = JSON.stringify(normalizedAnnouncements) !== JSON.stringify(saved.announcements ?? []);
        const normalizedEvents = (saved.events ?? []).map(withoutRetiredCardFields).map((event) => {
          if (saved.migrationCutoffAt && event.notificationPolicy === "historical_only") return { ...event, recoveredAfterGap: true };
          return event;
        });
        const retiredFieldsRemoved = JSON.stringify(normalizedEvents) !== JSON.stringify(saved.events ?? []);
        const normalizedActivityLogs = (saved.activityLogs ?? []).map((entry) => ({
          ...entry,
          message: entry.message.replace(/\uFFFD+/g, "")
        }));
        const activityLogsChanged = JSON.stringify(normalizedActivityLogs) !== JSON.stringify(saved.activityLogs ?? []);
        this.state = {
          ...saved,
          announcements: normalizedAnnouncements,
          events: normalizedEvents,
          activityLogs: normalizedActivityLogs,
          newsReadLogs: saved.newsReadLogs ?? [],
          newsHourlyStats: saved.newsHourlyStats ?? []
        };
        let stateChanged = needsActivityLogUpgrade || needsNewsLogUpgrade || needsNewsHourlyUpgrade || announcementsChanged || activityLogsChanged || retiredFieldsRemoved;
        if ((saved.repairVersion ?? 0) < 2) {
          this.state.repairVersion = 2;
          this.state.repairBaselineAt = (/* @__PURE__ */ new Date()).toISOString();
          this.state.repairedInventorySources = [];
          for (const event of this.state.events) {
            event.scheduledAt = this.state.inventory[event.exchange]?.[event.marketSymbol]?.scheduledAt ?? null;
            event.contractKind = /-\d{1,2}[A-Z]{3}\d{2}$/.test(event.marketSymbol) ? "delivery" : "perpetual";
            if (event.tradableAt && !event.pushedAt) event.pushStatus = "suppressed";
          }
          this.addActivityLog({ level: "info", type: "reliability_upgrade", message: "\u5386\u53F2\u672A\u786E\u8BA4\u5E02\u573A\u5DF2\u6062\u590D\u4F4E\u9891\u590D\u6838\uFF1B\u5386\u53F2\u8BB0\u5F55\u4EC5\u4FEE\u6B63\uFF0C\u4E0D\u96C6\u4E2D\u8865\u53D1\u65E7\u6D88\u606F\u3002" });
          stateChanged = true;
        }
        if ((this.state.repairVersion ?? 0) < REPAIR_VERSION) {
          const nowMs = Date.now();
          let retiredCount = 0;
          for (const event of this.state.events) {
            const policy = classifyBookMonitor(event, nowMs);
            if (policy.phase !== "retire") continue;
            this.retireBookMonitor(event, policy.reason, nowMs);
            retiredCount += 1;
          }
          this.state.repairVersion = REPAIR_VERSION;
          if (retiredCount) {
            this.addActivityLog({
              level: "info",
              type: "book_monitor_cleanup",
              message: `\u76D8\u53E3\u590D\u6838\u6E05\u7406\u5B8C\u6210\uFF1A\u5DF2\u505C\u6B62 ${retiredCount} \u4E2A\u8FC7\u671F\u6216\u65E0\u6548\u4EFB\u52A1\uFF1B\u5386\u53F2\u8BB0\u5F55\u548C\u63A8\u9001\u53BB\u91CD\u4FDD\u7559\u3002`
            });
          }
          stateChanged = true;
        }
        const prematureEventIds = /* @__PURE__ */ new Set();
        for (const event of this.state.events) {
          const inventoryItem = this.state.inventory[event.exchange]?.[event.marketSymbol];
          const scheduledMs = Date.parse(inventoryItem?.scheduledAt ?? "");
          const startedMs = Date.parse(event.monitorStartedAt);
          if (event.monitorStatus === "expired_no_book" && Number.isFinite(scheduledMs) && Number.isFinite(startedMs) && startedMs < scheduledMs) {
            prematureEventIds.add(event.id);
            if (inventoryItem) inventoryItem.monitorPending = true;
          }
        }
        if (prematureEventIds.size) {
          this.state.events = this.state.events.filter((event) => !prematureEventIds.has(event.id));
          stateChanged = true;
        }
        const visibleLogs = (this.state.activityLogs ?? []).filter((log) => log.type !== "book_verification_expired");
        if (visibleLogs.length !== (this.state.activityLogs ?? []).length) {
          this.state.activityLogs = visibleLogs;
          stateChanged = true;
        }
        for (const source of saved.sources ?? []) this.sources.set(`${source.exchange}:${source.kind}`, source);
        if (saved.runtime) this.runtime = saved.runtime;
        if (needsActivityLogUpgrade) {
          this.addActivityLog({
            level: "info",
            type: "activity_log_enabled",
            message: "\u7EBF\u4E0A\u8FD0\u884C\u65E5\u5FD7\u5DF2\u542F\u7528\uFF0C\u4EC5\u8BB0\u5F55\u91CD\u8981\u72B6\u6001\u53D8\u5316\u3002"
          });
        }
        if (stateChanged || !durable) await writeDurableState(ctx.storage, this.state, this.stateWriteCache);
      }
    });
  }
  addActivityLog(input) {
    const at = (/* @__PURE__ */ new Date()).toISOString();
    const entry = {
      id: `${at}:${crypto.randomUUID()}`,
      at,
      level: input.level,
      type: input.type,
      exchange: input.exchange ?? null,
      symbol: input.symbol ?? null,
      message: input.message
    };
    this.state.activityLogs ??= [];
    this.state.activityLogs.unshift(entry);
    this.state.activityLogs = this.state.activityLogs.slice(0, 120);
    console.log(JSON.stringify({ event: "exchange_monitor_activity", ...entry }));
  }
  async persist() {
    if (!this.isPrimaryMonitor()) return;
    const next = this.persistTail.catch(() => {
    }).then(async () => {
      this.state.sources = Array.from(this.sources.values());
      this.state.runtime = { ...this.runtime };
      await writeDurableState(this.ctx.storage, this.state, this.stateWriteCache);
      this.lastCheckpointAt = Date.now();
    });
    this.persistTail = next;
    await next;
  }
  async applyVerifiedAnnouncementRepairs() {
    const inbox = await this.env.STATE_KV.get("announcement-repair-inbox:v1", "json");
    if (!inbox || inbox.version !== 1 || !Array.isArray(inbox.items)) return false;
    let changed = false;
    this.state.announcementRepairs ??= {};
    for (const item of inbox.items.slice(0, 20)) {
      const code = item.article?.code;
      if (!code || !/^[a-f0-9]{32}$/i.test(code) || typeof item.article.body !== "string" || item.article.body.length > 5e5) continue;
      if (this.state.announcementRepairs[code] === item.revision) continue;
      const expected = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?articleCode=" + code;
      const retrieved = Date.parse(item.retrievedAt);
      if (item.sourceUrl !== expected || !Number.isFinite(retrieved) || retrieved > Date.now() + 6e4 || retrieved < Date.now() - 7 * 864e5 || !Number.isFinite(item.article?.publishDate) || item.article.publishDate > retrieved + 6e4) continue;
      const bytes = new TextEncoder().encode(JSON.stringify(item.article));
      const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
      if (hash !== item.revision) continue;
      const url = canonicalAnnouncementUrl("bn", "https://www.binance.com/en/support/announcement/" + code);
      const base = announcementRow("bn", item.article.title, url, item.article.publishDate, "listing");
      if (!base) continue;
      const repaired = parseBinanceDetail(base, item.article, item.retrievedAt).map((row) => ({ ...row, detailSource: "verified_repair" }));
      this.state.announcements = mergeAnnouncementBatch(this.state.announcements, repaired, item.retrievedAt);
      this.state.announcementRepairs[code] = item.revision;
      this.addActivityLog({
        level: "success",
        type: "announcement_evidence_repaired",
        exchange: "bn",
        message: "\u5DF2\u7528\u6838\u9A8C\u540E\u7684\u5E01\u5B89\u5B98\u65B9\u6B63\u6587\u8865\u56DE " + repaired.map((row) => row.symbol).filter(Boolean).join(" / ") + " \u516C\u544A\u53CA\u72EC\u7ACB\u8BA1\u5212\u65F6\u95F4\uFF1B\u4FDD\u7559\u539F\u53D1\u73B0\u65F6\u95F4\uFF0C\u672A\u8865\u53D1\u5386\u53F2\u901A\u77E5\u3002"
      });
      changed = true;
    }
    if (changed) await this.persist();
    return changed;
  }
  isPrimaryMonitor() {
    return this.ctx.id.equals(this.env.MONITOR.idFromName(PRIMARY_MONITOR_NAME));
  }
  recordNewsHourly(readLog) {
    const hour = `${readLog.requestStartedAt.slice(0, 13)}:00:00.000Z`;
    this.state.newsHourlyStats ??= [];
    let aggregate = this.state.newsHourlyStats.find((row) => row.hour === hour && row.exchange === readLog.exchange);
    if (!aggregate) {
      aggregate = {
        hour,
        exchange: readLog.exchange,
        scans: 0,
        successfulScans: 0,
        failedScans: 0,
        totalDurationMs: 0,
        maxDurationMs: 0,
        newItemCount: 0,
        non2xxCount: 0
      };
      this.state.newsHourlyStats.push(aggregate);
    }
    aggregate.scans += 1;
    aggregate.successfulScans += readLog.status === "ok" ? 1 : 0;
    aggregate.partialScans = (aggregate.partialScans ?? 0) + (readLog.status === "partial" ? 1 : 0);
    aggregate.failedScans += readLog.status === "error" ? 1 : 0;
    aggregate.totalDurationMs += readLog.durationMs;
    aggregate.maxDurationMs = Math.max(aggregate.maxDurationMs, readLog.durationMs);
    aggregate.newItemCount += readLog.newItemCount;
    aggregate.non2xxCount += readLog.statusCodes.filter((code) => code < 200 || code >= 300).length;
    const cutoff = Date.now() - NEWS_HOURLY_RETENTION_MS;
    this.state.newsHourlyStats = this.state.newsHourlyStats.filter((row) => Date.parse(row.hour) >= cutoff).sort((left, right) => Date.parse(right.hour) - Date.parse(left.hour));
  }
  async claimLocalPush(pushKey, eventId) {
    const storageKey = `${PUSH_LOCK_PREFIX}${pushKey}`;
    return await this.ctx.storage.transaction(async (transaction) => {
      const existing = await transaction.get(storageKey);
      const existingAt = Date.parse(existing?.updatedAt ?? "");
      const now = Date.now();
      if (existing && Number.isFinite(existingAt)) {
        if (existing.status === "sent" && now - existingAt < PUSH_DEDUP_MS) {
          return { claimed: false, token: null, sentAt: existing.updatedAt };
        }
        if (existing.status === "sending" && now - existingAt < PUSH_SENDING_STALE_MS) {
          return { claimed: false, token: null, sentAt: null };
        }
      }
      const token = crypto.randomUUID();
      await transaction.put(storageKey, {
        status: "sending",
        token,
        eventId,
        updatedAt: new Date(now).toISOString()
      });
      return { claimed: true, token, sentAt: null };
    });
  }
  async claimGlobalPush(pushKey, eventId) {
    if (this.isPrimaryMonitor()) return await this.claimLocalPush(pushKey, eventId);
    const response = await monitorStub(this.env).fetch(new Request("https://monitor.internal/internal/push/claim", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pushKey, eventId })
    }));
    if (!response.ok) throw new Error(`global push claim failed: HTTP ${response.status}`);
    return await response.json();
  }
  async settleLocalPush(pushKey, token, eventId, sent) {
    const storageKey = `${PUSH_LOCK_PREFIX}${pushKey}`;
    await this.ctx.storage.transaction(async (transaction) => {
      const existing = await transaction.get(storageKey);
      if (!existing || existing.token !== token) return;
      if (!sent) {
        await transaction.delete(storageKey);
        return;
      }
      await transaction.put(storageKey, {
        status: "sent",
        token,
        eventId,
        updatedAt: (/* @__PURE__ */ new Date()).toISOString()
      });
    });
  }
  async settleGlobalPush(pushKey, token, eventId, sent) {
    if (this.isPrimaryMonitor()) {
      await this.settleLocalPush(pushKey, token, eventId, sent);
      return;
    }
    const response = await monitorStub(this.env).fetch(new Request("https://monitor.internal/internal/push/settle", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pushKey, token, eventId, sent })
    }));
    if (!response.ok) throw new Error(`global push settle failed: HTTP ${response.status}`);
  }
  async fetchRemoteContracts(exchange) {
    const request = new Request(`https://monitor.internal/proxy/contracts?exchange=${exchange}`);
    const response = exchange === "bg" || exchange === "bn" ? await this.fetch(request) : await exchangeFetchStub(this.env, exchange).fetch(request);
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    this.contractDiagnostics.set(exchange, {
      dataSource: payload.contracts?.[0]?.dataSource ?? "official_rest",
      dataAsOf: payload.cachedAt ?? null,
      cacheStatus: payload.cacheStatus ?? "unknown",
      partial: (payload.contracts ?? []).some((row) => row.coverage === "partial")
    });
    return payload.contracts ?? [];
  }
  async fetchRemoteNews(exchange) {
    const request = new Request(`https://monitor.internal/proxy/news?exchange=${exchange}`);
    const response = exchange === "gate" ? await this.fetch(request) : await exchangeFetchStub(this.env, exchange).fetch(request);
    const payload = await response.json();
    return {
      rows: payload.announcements ?? [],
      trace: { attempts: payload.httpAttempts ?? [] },
      warning: payload.warning ?? null,
      error: response.ok ? null : payload.error ?? `HTTP ${response.status}`
    };
  }
  async fetchRemoteBook(exchange, marketSymbol, marketType = "contract") {
    if (exchange === "bn" && marketType === "contract") return this.readBinanceBook(marketSymbol);
    const query = new URLSearchParams({ exchange, marketSymbol, marketType });
    const request = new Request(`https://monitor.internal/proxy/book?${query}`);
    const response = exchange === "bg" || exchange === "bn" ? await this.fetch(request) : await exchangeFetchStub(this.env, exchange).fetch(request);
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    return payload.book ?? null;
  }
  async readBinanceBook(marketSymbol) {
    if (this.binanceBooks && Date.now() - this.binanceBooks.at < BOOK_INTERVAL_MS) return this.binanceBooks.books.get(marketSymbol) ?? null;
    if (Date.now() < this.binanceBookRetryAt) throw new Error(this.binanceBookError ?? "Binance \u5B98\u65B9\u76D8\u53E3\u9000\u907F\u4E2D");
    if (!this.binanceBookFlight) {
      this.binanceBookFlight = (async () => {
        try {
          const rows = await fetchJson("https://fapi.binance.com/fapi/v1/ticker/bookTicker", {}, 4e3);
          if (!Array.isArray(rows) || !rows.length) throw new Error("Binance \u5B98\u65B9\u6279\u91CF\u76D8\u53E3\u4E3A\u7A7A");
          const books = /* @__PURE__ */ new Map();
          for (const row of rows) {
            const bid = asNumber(row.bidPrice), ask = asNumber(row.askPrice);
            if (typeof row.symbol === "string" && bid && ask && ask >= bid) books.set(row.symbol.toUpperCase(), { bid, ask });
          }
          if (!books.size) throw new Error("Binance \u5B98\u65B9\u6279\u91CF\u76D8\u53E3\u683C\u5F0F\u5F02\u5E38");
          this.binanceBooks = { at: Date.now(), books };
          this.binanceBookRetryAt = 0;
          this.binanceBookError = null;
          this.binanceBookFailures = 0;
          return books;
        } catch (error) {
          this.binanceBookFailures = (this.binanceBookFailures ?? 0) + 1;
          const delay = Math.min(6e4, 5e3 * 2 ** Math.min(this.binanceBookFailures - 1, 4));
          this.binanceBookRetryAt = Date.now() + Math.max(delay, error instanceof UpstreamHttpError ? error.retryAfterMs : 0);
          this.binanceBookError = String(error);
          throw error;
        } finally {
          this.binanceBookFlight = null;
        }
      })();
    }
    return (await this.binanceBookFlight).get(marketSymbol) ?? null;
  }
  upsertSource(exchange, kind, status, count, message, diagnostics = {}) {
    const key = `${exchange}:${kind}`;
    const previous = this.sources.get(key);
    const failureThreshold = kind === "news" ? 2 : 3;
    const consecutiveFailures = status === "error" ? (previous?.consecutiveFailures ?? 0) + 1 : 0;
    const confirmedError = status === "error" && consecutiveFailures >= failureThreshold;
    const effectiveStatus = status === "error" ? confirmedError ? "error" : previous?.status === "ok" ? "ok" : "degraded" : status;
    const effectiveMessage = status === "error" && !confirmedError ? `\u77AC\u65F6\u5931\u8D25 ${consecutiveFailures}/${failureThreshold}\uFF1A${message ?? "\u672A\u77E5\u9519\u8BEF"}` : message;
    const reason = (message ?? "").replace(/；下次尝试\s+[^；]+/g, "").replace(/；数据时间\s+[^；]+/g, "");
    const issue = status === "error" ? confirmedError ? "error" : "transient" : status === "degraded" ? "degraded" : "ok";
    const progressOnly = kind === "news" && status === "degraded" && /^目录已读取；/.test(reason) && !/读取失败/.test(reason);
    const signatureReason = progressOnly ? "news_information_pending" : reason.replace(/\d+\s*篇/g, "\u82E5\u5E72\u7BC7");
    const activitySignature = issue === "ok" ? "ok" : `${issue}:${signatureReason}`;
    this.sources.set(key, {
      exchange,
      kind,
      status: effectiveStatus,
      count: status === "error" && previous ? previous.count : count,
      lastCheckedAt: (/* @__PURE__ */ new Date()).toISOString(),
      message: effectiveMessage,
      consecutiveFailures,
      activitySignature,
      ...diagnostics
    });
    const name = `${exchangeName(exchange)} ${kind === "contracts" ? "\u5408\u7EA6\u6E90" : "\u516C\u544A\u6E90"}`;
    if (issue !== "ok" && previous?.activitySignature !== activitySignature) {
      const informationOnly = kind === "news" && status === "degraded" && /正文已读取/.test(message ?? "") && !/读取失败|待抓取/.test(message ?? "");
      this.addActivityLog({
        level: confirmedError ? "error" : "warning",
        type: confirmedError ? "source_error" : informationOnly ? "news_information_unconfirmed" : status === "degraded" && kind === "news" ? "news_detail_incomplete" : "source_degraded",
        exchange,
        message: `${name}${informationOnly ? "\u4FE1\u606F\u5F85\u786E\u8BA4\uFF08\u4E0D\u662F\u7F51\u7EDC\u4E2D\u65AD\uFF09" : issue === "transient" ? "\u8BF7\u6C42\u6682\u65F6\u5931\u8D25\uFF0C\u7B49\u5F85\u91CD\u8BD5" : "\u5F02\u5E38\u6216\u6570\u636E\u4E0D\u5B8C\u6574"}\uFF1A${effectiveMessage ?? "\u539F\u56E0\u672A\u77E5"}`
      });
      return true;
    }
    if ((previous?.status === "error" || previous?.status === "degraded" || previous?.consecutiveFailures) && status === "ok") {
      this.addActivityLog({
        level: "success",
        type: "source_recovered",
        exchange,
        message: `${name}\u5DF2\u6062\u590D\uFF0C\u5F53\u524D\u68C0\u67E5\u901A\u8FC7\u3002`
      });
      return true;
    }
    return false;
  }
  findMatchingAnnouncement(exchange, symbol, scheduledAt = null, marketType = "contract", preferredUrl) {
    const candidates = this.state.announcements.filter(
      (row) => row.exchange === exchange && row.action === "listing" && row.marketType === marketType && (row.symbol === symbol || row.symbols?.includes(symbol) || titleHasSymbol(row.title, symbol)) && (!scheduledAt || !row.scheduledAt || Math.abs(Date.parse(row.scheduledAt) - Date.parse(scheduledAt)) < 864e5)
    );
    return candidates.find((row) => row.url === (preferredUrl ? canonicalAnnouncementUrl(exchange, preferredUrl) : null)) ?? candidates.sort((a, b) => Number(Boolean(b.detailCheckedAt)) - Number(Boolean(a.detailCheckedAt)) || (Date.parse(b.publishedAt ?? "") || 0) - (Date.parse(a.publishedAt ?? "") || 0) || a.url.localeCompare(b.url))[0] ?? null;
  }
  reconcileAnnouncements() {
    let changed = false;
    const notices = /* @__PURE__ */ new Map();
    for (const event of this.state.events) {
      const match = this.findMatchingAnnouncement(event.exchange, event.symbol, event.scheduledAt, event.marketType, event.announcementUrl);
      if (event.announcementMatched === Boolean(match) && event.announcementUrl === (match?.url ?? null) && event.publishedAt === (match?.publishedAt ?? null)) continue;
      const linkChanged = event.announcementMatched !== Boolean(match) || (event.announcementUrl ? canonicalAnnouncementUrl(event.exchange, event.announcementUrl) : null) !== (match?.url ?? null);
      event.announcementMatched = Boolean(match);
      event.announcementTitle = match?.title ?? null;
      event.announcementUrl = match?.url ?? null;
      event.publishedAt = match?.publishedAt ?? null;
      if (event.tradableAt) event.marketMessage = match ? "\u5E02\u573A\u5DF2\u5F00\u653E\uFF0C\u516C\u544A\u5DF2\u5339\u914D" : "\u5E02\u573A\u5DF2\u5F00\u653E\uFF0C\u672A\u5339\u914D\u516C\u544A";
      event.updatedAt = (/* @__PURE__ */ new Date()).toISOString();
      if (linkChanged) {
        const key = `${event.exchange}:${match?.url ?? "unmatched"}`;
        const notice = notices.get(key) ?? { exchange: event.exchange, symbols: /* @__PURE__ */ new Set(), matched: Boolean(match) };
        notice.symbols.add(event.symbol);
        notices.set(key, notice);
      }
      changed = true;
    }
    for (const notice of notices.values()) this.addActivityLog({
      level: notice.matched ? "success" : "warning",
      type: "announcement_matched",
      exchange: notice.exchange,
      message: `${exchangeName(notice.exchange)} ${[...notice.symbols].join(" / ")} ${notice.matched ? "\u5DF2\u8865\u5145\u516C\u544A\u4FE1\u606F" : "\u5DF2\u64A4\u9500\u7F3A\u4E4F\u5E01\u79CD\u8BC1\u636E\u7684\u516C\u544A\u5339\u914D"}\u3002`
    });
    return changed;
  }
  async scanNews(scanStartedAt = (/* @__PURE__ */ new Date()).toISOString()) {
    this.runtime.lastNewsScanAt = scanStartedAt;
    try {
      await this.applyVerifiedAnnouncementRepairs();
    } catch (error) {
      console.error(JSON.stringify({ event: "announcement_repair_failed", error: String(error) }));
    }
    this.state.newsRetryState ??= {};
    const exchanges = Object.keys(NEWS_FETCHERS).filter((exchange) => Date.now() >= (this.state.newsRetryState?.[exchange]?.retryAt ?? 0));
    const outcomes = await Promise.all(exchanges.map(async (exchange) => {
      const requestStartedAt = (/* @__PURE__ */ new Date()).toISOString();
      try {
        const remote = await this.fetchRemoteNews(exchange);
        return {
          exchange,
          rows: remote.rows,
          trace: remote.trace,
          requestStartedAt,
          responseCompletedAt: (/* @__PURE__ */ new Date()).toISOString(),
          error: remote.error,
          warning: remote.warning
        };
      } catch (error) {
        return {
          exchange,
          rows: [],
          trace: { attempts: [] },
          requestStartedAt,
          responseCompletedAt: (/* @__PURE__ */ new Date()).toISOString(),
          error: String(error),
          warning: null
        };
      }
    }));
    let existing = new Map(normalizePersistedAnnouncements(this.state.announcements).map((row) => [row.key, row]));
    let changed = false;
    for (const outcome of outcomes) {
      const { exchange, rows, trace, requestStartedAt, responseCompletedAt, error, warning } = outcome;
      const durationMs = Math.max(0, Date.parse(responseCompletedAt) - Date.parse(requestStartedAt));
      const statusCodes = trace.attempts.map((attempt) => attempt.statusCode).filter((statusCode2) => statusCode2 !== null);
      const statusCode = [...statusCodes].reverse().find((code) => code >= 200 && code < 400) ?? statusCodes.at(-1) ?? null;
      let sourceMessage = error ?? warning;
      if (error) {
        const failures = (this.state.newsRetryState[exchange]?.failures ?? 0) + 1;
        const blocked = statusCodes.some((code) => code === 403 || code === 429);
        const delay = blocked ? Math.min(15 * 6e4, NEWS_INTERVAL_MS * 2 ** Math.min(failures - 1, 4)) : NEWS_INTERVAL_MS;
        const requestedDelay = Math.max(0, ...trace.attempts.map((attempt) => attempt.retryAfterMs ?? 0));
        const retryAt = Date.now() + Math.max(delay, requestedDelay);
        this.state.newsRetryState[exchange] = { failures, retryAt };
        sourceMessage = `${error}\uFF1B\u4E0B\u6B21\u5C1D\u8BD5 ${displayBeijing(new Date(retryAt).toISOString())}`;
      } else {
        delete this.state.newsRetryState[exchange];
      }
      let newItemCount = 0;
      const newPublishedAt = [];
      this.state.newsSeenUrls ??= {};
      const knownUrls = /* @__PURE__ */ new Set([...Object.keys(this.state.newsSeenUrls), ...[...existing.values()].map((row) => row.exchange + ":" + canonicalAnnouncementUrl(row.exchange, row.url))]);
      for (const item of rows) {
        const identity = item.exchange + ":" + canonicalAnnouncementUrl(item.exchange, item.url);
        if (!knownUrls.has(identity)) {
          knownUrls.add(identity);
          newItemCount += 1;
          newPublishedAt.push(item.publishedAt);
        }
        this.state.newsSeenUrls[identity] ??= responseCompletedAt;
      }
      this.state.newsSeenUrls = Object.fromEntries(Object.entries(this.state.newsSeenUrls).slice(-1e4));
      existing = new Map(mergeAnnouncementBatch([...existing.values()], rows, responseCompletedAt).map((row) => [row.key, row]));
      const firstSeenAt = newItemCount > 0 ? responseCompletedAt : null;
      const readLog = {
        id: `${responseCompletedAt}:${exchange}:${crypto.randomUUID()}`,
        exchange,
        requestStartedAt,
        responseCompletedAt,
        firstSeenAt,
        durationMs,
        statusCode,
        statusCodes,
        itemCount: rows.length,
        newItemCount,
        status: error ? "error" : warning ? "partial" : "ok",
        message: sourceMessage
      };
      this.state.newsReadLogs ??= [];
      this.state.newsReadLogs.unshift(readLog);
      this.state.newsReadLogs = this.state.newsReadLogs.slice(0, NEWS_RAW_LOG_LIMIT);
      this.recordNewsHourly(readLog);
      console.log(JSON.stringify({ event: "exchange_news_source_read", ...readLog, httpAttempts: trace.attempts }));
      const diagnostics = {
        requestStartedAt,
        responseCompletedAt,
        firstSeenAt: firstSeenAt ?? this.sources.get(`${exchange}:news`)?.firstSeenAt ?? null,
        durationMs,
        statusCode,
        statusCodes
      };
      changed = this.upsertSource(
        exchange,
        "news",
        error ? "error" : warning ? "degraded" : "ok",
        rows.length,
        sourceMessage,
        diagnostics
      ) || changed;
      if (newItemCount > 0) {
        const historicalCount = newPublishedAt.filter((value) => Number.isFinite(Date.parse(value ?? "")) && Date.parse(value) < Date.parse(responseCompletedAt) - 864e5).length;
        const delays = newPublishedAt.map((publishedAt) => Date.parse(responseCompletedAt) - Date.parse(publishedAt ?? "")).filter((delay) => Number.isFinite(delay) && delay >= 0 && delay <= 864e5);
        const maxDelayMs = delays.length ? Math.max(...delays) : null;
        this.addActivityLog({
          level: maxDelayMs !== null && maxDelayMs > 3 * 6e4 ? "warning" : "success",
          type: "news_first_seen",
          exchange,
          message: `${exchangeName(exchange)} \u9996\u6B21\u6536\u5F55 ${newItemCount} \u6761${historicalCount ? `\uFF08\u5386\u53F2\u8865\u67E5 ${historicalCount} \u6761\uFF0C\u4E0D\u8BA1\u5165\u5B9E\u65F6\u5EF6\u8FDF\uFF09` : ""}\uFF1B\u8BFB\u53D6 ${durationMs}ms\uFF1BHTTP ${statusCodes.join("/") || "\u65E0\u54CD\u5E94\u7801"}${maxDelayMs !== null ? `\uFF1B\u8FD1\u671F\u516C\u544A\u6700\u5927\u53D1\u5E03\u5EF6\u8FDF ${Math.round(maxDelayMs / 1e3)}\u79D2` : ""}\u3002`
        });
      }
      changed = true;
    }
    changed = this.upsertSource("hl", "news", "market_only", 0, "Hyperliquid \u65E0\u516C\u544A\u76EE\u5F55\uFF1B\u6309\u5E02\u573A\u5F00\u653E\u72B6\u6001\u8BB0\u5F55\u3002") || changed;
    if (changed) {
      this.state.announcements = Array.from(existing.values()).sort((a, b) => Date.parse(b.publishedAt ?? b.fetchedAt) - Date.parse(a.publishedAt ?? a.fetchedAt)).slice(0, 500);
    }
    return this.reconcileAnnouncements() || changed;
  }
  createMonitorEvent(exchange, contract, firstDetectedAt, monitorStartedAt = firstDetectedAt, marketType = "contract") {
    if (this.state.events.some((row) => row.exchange === exchange && row.marketSymbol === contract.marketSymbol && row.marketType === marketType && ["verifying", "waiting_book", "tradable"].includes(row.monitorStatus))) return;
    const match = this.findMatchingAnnouncement(exchange, contract.symbol, contract.scheduledAt, marketType);
    const historical = Date.parse(contract.scheduledAt ?? firstDetectedAt) <= Date.parse(this.state.migrationCutoffAt ?? "") || Boolean(this.state.migrationPendingSources?.includes(exchange));
    this.state.events.unshift({
      ...historical ? { notificationPolicy: "historical_only", recoveredAfterGap: true } : {},
      id: `${exchange}:${marketType === "spot" ? "spot:" : ""}${contract.marketSymbol}:${monitorStartedAt}`,
      exchange,
      exchangeName: exchangeName(exchange),
      symbol: contract.symbol,
      marketSymbol: contract.marketSymbol,
      marketType,
      contractKind: contract.contractKind ?? "perpetual",
      scheduledAt: contract.scheduledAt,
      nextBookCheckAt: monitorStartedAt,
      announcementTitle: match?.title ?? null,
      announcementUrl: match?.url ?? null,
      publishedAt: match?.publishedAt ?? null,
      firstDetectedAt,
      tradableAt: null,
      firstBid: null,
      firstAsk: null,
      monitorStartedAt,
      monitorEndsAt: new Date(Date.parse(monitorStartedAt) + BOOK_MONITOR_MS).toISOString(),
      monitorStatus: "verifying",
      bookChecks: 0,
      announcementMatched: Boolean(match),
      marketMessage: match ? "\u65B0\u589E\u5408\u7EA6\u5DF2\u53D1\u73B0\uFF0C\u7B49\u5F85\u9996\u6B21\u6709\u6548\u76D8\u53E3" : "\u65B0\u589E\u5408\u7EA6\u5DF2\u53D1\u73B0\uFF0C\u5C1A\u672A\u5339\u914D\u516C\u544A",
      pushedAt: null,
      createdAt: monitorStartedAt,
      updatedAt: monitorStartedAt
    });
    this.state.events = this.state.events.slice(0, 240);
    this.addActivityLog({
      level: "info",
      type: "new_contract_detected",
      exchange,
      symbol: contract.symbol,
      message: historical ? `${exchangeName(exchange)} ${contract.marketSymbol} \u8865\u67E5\u5386\u53F2\u76D8\u53E3\uFF0C\u4E0D\u8865\u63A8\u3002` : `${exchangeName(exchange)} \u53D1\u73B0${marketType === "spot" ? "\u73B0\u8D27" : contract.contractKind === "delivery" ? "\u4EA4\u5272\u5408\u7EA6\u65B0\u589E\u671F\u9650" : "\u65B0\u589E\u5408\u7EA6"} ${contract.marketSymbol}\uFF0C\u5F00\u59CB 1 \u79D2\u76D8\u53E3\u6838\u9A8C\u3002`
    });
  }
  async scanContracts() {
    const now = (/* @__PURE__ */ new Date()).toISOString();
    this.runtime.lastContractScanAt = now;
    const settled = await Promise.allSettled(EXCHANGES.map(async ({ code }) => [code, await this.fetchRemoteContracts(code)]));
    let changed = false;
    for (let index = 0; index < settled.length; index += 1) {
      const exchange = EXCHANGES[index].code;
      const result = settled[index];
      if (result.status === "rejected") {
        changed = this.upsertSource(exchange, "contracts", "error", 0, String(result.reason)) || changed;
        continue;
      }
      const contracts = result.value[1].filter((row) => row.symbol && row.marketSymbol);
      if (!contracts.length) {
        changed = this.upsertSource(exchange, "contracts", "error", 0, "\u5408\u7EA6\u5217\u8868\u4E3A\u7A7A\uFF0C\u672A\u8986\u76D6\u5DF2\u6709\u57FA\u7EBF\u3002") || changed;
        continue;
      }
      const inventory = this.state.inventory[exchange];
      if (this.state.migrationPendingSources?.includes(exchange)) {
        const diagnostic2 = this.contractDiagnostics.get(exchange);
        if (diagnostic2?.partial || diagnostic2?.cacheStatus && diagnostic2.cacheStatus !== "fresh") {
          this.upsertSource(exchange, "contracts", "degraded", contracts.length, "\u8FC1\u79FB\u57FA\u7EBF\u7B49\u5F85\u5B8C\u6574\u3001\u65B0\u9C9C\u7684\u5408\u7EA6\u5217\u8868\uFF1B\u6682\u4E0D\u53D1\u9001\u8BE5\u6765\u6E90\u65B0\u5408\u7EA6\u63D0\u9192\u3002");
          continue;
        }
        for (const contract of contracts) {
          const previous = inventory[contract.marketSymbol];
          inventory[contract.marketSymbol] = {
            ...previous,
            symbol: contract.symbol,
            firstSeenAt: previous?.firstSeenAt ?? now,
            scheduledAt: contract.scheduledAt,
            marketStatus: contract.marketStatus,
            assetType: contract.assetType,
            contractKind: contract.contractKind ?? "perpetual",
            monitorPending: Date.parse(contract.scheduledAt ?? "") > Date.now()
          };
        }
        this.state.migrationPendingSources = this.state.migrationPendingSources.filter((code) => code !== exchange);
        this.upsertSource(exchange, "contracts", "ok", contracts.length, "\u672C\u5730\u8FC1\u79FB\u57FA\u7EBF\u5DF2\u6838\u5BF9\uFF1B\u65E2\u6709\u5408\u7EA6\u53EA\u8865\u67E5\u3001\u4E0D\u8865\u63A8\u3002");
        this.addActivityLog({ level: "success", type: "migration_baseline", exchange, message: `${exchangeName(exchange)} \u5DF2\u6838\u5BF9 ${contracts.length} \u4E2A\u5408\u7EA6\uFF0C\u5386\u53F2\u4E0D\u8865\u63A8\u3002` });
        changed = true;
        continue;
      }
      const initialized = Object.keys(inventory).length > 0;
      const repairingBitget = exchange === "bg" && Boolean(this.state.repairBaselineAt) && !this.state.repairedInventorySources?.includes(exchange);
      for (const contract of contracts) {
        const existing = inventory[contract.marketSymbol];
        if (existing) {
          const nextScheduledAt = contract.scheduledAt ?? existing.scheduledAt ?? null;
          const nextMarketStatus = contract.marketStatus ?? existing.marketStatus ?? null;
          const nextAssetType = mergedAssetType(contract.assetType, existing.assetType);
          const scheduledMs = Date.parse(nextScheduledAt ?? "");
          const monitorHistory = this.state.events.filter(
            (event) => event.exchange === exchange && event.marketSymbol === contract.marketSymbol
          );
          const prematureEventIds = new Set(
            monitorHistory.filter(
              (event) => event.monitorStatus !== "tradable" && Number.isFinite(scheduledMs) && Date.parse(event.monitorStartedAt) < scheduledMs
            ).map((event) => event.id)
          );
          if (prematureEventIds.size) {
            this.state.events = this.state.events.filter((event) => !prematureEventIds.has(event.id));
            existing.monitorPending = true;
            changed = true;
          }
          if (existing.scheduledAt !== nextScheduledAt || existing.marketStatus !== nextMarketStatus || existing.assetType !== nextAssetType) {
            existing.scheduledAt = nextScheduledAt;
            existing.marketStatus = nextMarketStatus;
            existing.assetType = nextAssetType;
            changed = true;
          }
          existing.contractKind = contract.contractKind ?? existing.contractKind ?? "perpetual";
          const firstSeenMs = Date.parse(existing.firstSeenAt);
          const hasValidMonitorHistory = monitorHistory.some(
            (event) => event.monitorStatus === "tradable" || Date.parse(event.monitorStartedAt) >= scheduledMs
          );
          const scheduledListingReached = Number.isFinite(scheduledMs) && Number.isFinite(firstSeenMs) && scheduledMs >= firstSeenMs && scheduledMs <= Date.now() && !hasValidMonitorHistory;
          if ((existing.monitorPending || scheduledListingReached) && scheduledMonitorCanStart(nextScheduledAt)) {
            existing.monitorPending = false;
            this.createMonitorEvent(
              exchange,
              { ...contract, scheduledAt: nextScheduledAt, marketStatus: nextMarketStatus, assetType: nextAssetType },
              existing.firstSeenAt,
              now
            );
            changed = true;
          }
          continue;
        }
        const startMonitorNow = initialized && scheduledMonitorCanStart(contract.scheduledAt);
        inventory[contract.marketSymbol] = {
          symbol: contract.symbol,
          firstSeenAt: now,
          scheduledAt: contract.scheduledAt,
          marketStatus: contract.marketStatus,
          assetType: contract.assetType,
          contractKind: contract.contractKind ?? "perpetual",
          monitorPending: initialized && !startMonitorNow
        };
        if (startMonitorNow) {
          this.createMonitorEvent(exchange, contract, now);
          if (repairingBitget && Date.parse(contract.scheduledAt ?? "") < Date.parse(this.state.repairBaselineAt) - 5 * 6e4) {
            const event = this.state.events.find((row) => row.exchange === exchange && row.marketSymbol === contract.marketSymbol);
            if (event) {
              event.notificationPolicy = "historical_only";
              event.recoveredAfterGap = true;
            }
          }
        }
        changed = true;
      }
      for (const [marketSymbol, item] of Object.entries(inventory)) {
        const scheduledMs = Date.parse(item.scheduledAt ?? "");
        const firstSeenMs = Date.parse(item.firstSeenAt);
        if (!Number.isFinite(scheduledMs) || !Number.isFinite(firstSeenMs) || scheduledMs < firstSeenMs || scheduledMs > Date.now()) continue;
        const history = this.state.events.filter(
          (event) => event.exchange === exchange && event.marketSymbol === marketSymbol
        );
        const prematureIds = new Set(
          history.filter((event) => event.monitorStatus !== "tradable" && Date.parse(event.monitorStartedAt) < scheduledMs).map((event) => event.id)
        );
        if (prematureIds.size) {
          this.state.events = this.state.events.filter((event) => !prematureIds.has(event.id));
          changed = true;
        }
        const hasValidHistory = history.some(
          (event) => event.monitorStatus === "tradable" || Date.parse(event.monitorStartedAt) >= scheduledMs
        );
        if (hasValidHistory) continue;
        item.monitorPending = false;
        this.createMonitorEvent(exchange, {
          symbol: item.symbol,
          marketSymbol,
          scheduledAt: item.scheduledAt ?? null,
          marketStatus: item.marketStatus ?? null,
          assetType: item.assetType ?? "unknown"
        }, item.firstSeenAt, now);
        changed = true;
      }
      const diagnostic = this.contractDiagnostics.get(exchange);
      const degraded = diagnostic?.partial || diagnostic?.cacheStatus && diagnostic.cacheStatus !== "fresh";
      changed = this.upsertSource(
        exchange,
        "contracts",
        degraded ? "degraded" : "ok",
        contracts.length,
        degraded ? `\u6570\u636E\u964D\u7EA7\uFF1A${diagnostic?.dataSource} / ${diagnostic?.cacheStatus}\uFF1B\u6570\u636E\u65F6\u95F4 ${diagnostic?.dataAsOf ?? "\u672A\u77E5"}` : initialized ? null : "\u5DF2\u5EFA\u7ACB\u5168\u91CF\u57FA\u7EBF\uFF1B\u9996\u6B21\u521D\u59CB\u5316\u4E0D\u89E6\u53D1\u65B0\u5408\u7EA6\u544A\u8B66\u3002"
      ) || changed;
      if (diagnostic) Object.assign(this.sources.get(`${exchange}:contracts`), diagnostic);
      if (repairingBitget && !degraded) {
        this.state.repairedInventorySources ??= [];
        this.state.repairedInventorySources.push(exchange);
        this.addActivityLog({ level: "success", type: "inventory_repaired", exchange, message: `Bitget \u5B98\u65B9\u5217\u8868\u5DF2\u91CD\u65B0\u6838\u5BF9\uFF0C\u5171 ${contracts.length} \u4E2A\u5408\u7EA6\uFF1B\u5386\u53F2\u7F3A\u9879\u4EC5\u4FEE\u6B63\u8BB0\u5F55\u3002` });
      }
    }
    return changed;
  }
  deferPush(event, error) {
    const exhausted = (event.pushAttempts ?? 0) >= PUSH_RETRY_LIMIT;
    event.pushStatus = exhausted ? "failed" : "pending";
    event.pushLastError = error;
    event.nextPushAt = exhausted ? null : new Date(Date.now() + Math.min(9e5, 3e4 * 2 ** Math.max(0, (event.pushAttempts ?? 1) - 1))).toISOString();
  }
  async pushTradable(event) {
    if (!this.state.running || !await executionAllowed(this.env)) return;
    if (this.state.migrationPendingSources?.includes(event.exchange) || Date.parse(event.scheduledAt ?? event.firstDetectedAt) <= Date.parse(this.state.migrationCutoffAt ?? "")) {
      event.notificationPolicy = "historical_only";
    }
    if (event.notificationPolicy === "historical_only") {
      event.pushStatus = "suppressed";
      return;
    }
    if (event.pushStatus === "failed" || event.pushStatus === "suppressed") return;
    if (event.nextPushAt && Date.parse(event.nextPushAt) > Date.now()) return;
    const pushDate = beijingDateKey(event.firstDetectedAt) ?? event.firstDetectedAt.slice(0, 10);
    const pushKey = event.exchange + ":" + (event.marketType === "spot" ? "spot:" : "") + event.marketSymbol + ":" + pushDate;
    const successful = this.state.events.find((row) => row.id !== event.id && row.exchange === event.exchange && row.marketSymbol === event.marketSymbol && row.marketType === event.marketType && row.pushedAt && Date.parse(row.pushedAt) >= Date.now() - PUSH_DEDUP_MS);
    if (event.pushedAt || successful) {
      event.pushedAt ??= successful?.pushedAt ?? null;
      event.pushStatus = "sent";
      return;
    }
    if (this.pushInFlight.has(pushKey)) return;
    event.pushAttempts = (event.pushAttempts ?? 0) + 1;
    event.pushStatus = "pending";
    await this.persist();
    let claim;
    try {
      claim = await this.claimGlobalPush(pushKey, event.id);
    } catch (error) {
      this.deferPush(event, "\u63A8\u9001\u9501\u6682\u4E0D\u53EF\u7528\uFF1A" + String(error));
      this.addActivityLog({ level: "error", type: "push_lock_error", exchange: event.exchange, symbol: event.symbol, message: event.exchangeName + " " + event.symbol + " \u63A8\u9001\u9501\u5F02\u5E38\uFF0C\u5DF2\u4FDD\u7559\u5F85\u53D1\u9001\u8BB0\u5F55\u3002" });
      await this.persist();
      return;
    }
    if (!claim.claimed || !claim.token) {
      if (claim.sentAt) {
        event.pushedAt = claim.sentAt;
        event.pushStatus = "sent";
      } else {
        event.pushAttempts -= 1;
        event.nextPushAt = new Date(Date.now() + PUSH_SENDING_STALE_MS).toISOString();
      }
      await this.persist();
      return;
    }
    this.pushInFlight.add(pushKey);
    const market = event.marketType === "spot" ? "\u73B0\u8D27" : event.contractKind === "delivery" ? "\u4EA4\u5272\u5408\u7EA6\uFF08\u65B0\u589E\u671F\u9650\uFF09" : "\u65B0\u5408\u7EA6";
    const title = market + "\u53EF\u4EA4\u6613 " + event.exchangeName + " " + event.symbol;
    const body = [
      "\u9996\u6B21\u76D8\u53E3\u786E\u8BA4\uFF1A" + displayBeijing(event.tradableAt),
      "\u9996\u6B21\u53D1\u73B0\u65F6\u95F4\uFF1A" + displayBeijing(event.firstDetectedAt),
      "\u65B0\u95FB\u53D1\u5E03\u65F6\u95F4\uFF1A" + displayBeijing(event.publishedAt, event.announcementMatched ? "\u6765\u6E90\u672A\u63D0\u4F9B\u65F6\u95F4" : "\u672A\u5339\u914D\u516C\u544A"),
      "\u76D8\u53E3\uFF1A" + event.firstBid + " / " + event.firstAsk,
      event.marketMessage
    ].join("\n");
    let status = "error";
    let message = null;
    try {
      if (!this.env.BARK_WEBHOOK_URL) throw new Error("\u63A8\u9001\u901A\u9053\u672A\u914D\u7F6E");
      if (!this.state.running || !await executionAllowed(this.env)) throw new Error("\u6267\u884C\u7AEF\u5DF2\u6682\u505C\uFF0C\u7981\u6B62\u53D1\u9001");
      const url = new URL(this.env.BARK_WEBHOOK_URL.replace(/\/$/, "") + "/" + encodeURIComponent(title) + "/" + encodeURIComponent(body));
      url.searchParams.set("group", "\u4EA4\u6613\u6240\u65B0\u95FB\u76D1\u63A7");
      url.searchParams.set("id", "listing-" + pushKey);
      if (this.env.PUBLIC_SITE_ORIGIN) url.searchParams.set("url", this.env.PUBLIC_SITE_ORIGIN);
      const response = await fetch(url, { signal: AbortSignal.timeout(8e3) });
      if (!response.ok) throw new Error("\u63A8\u9001 HTTP " + response.status);
      const result = await response.json();
      if (Number(result.code) !== 200) throw new Error("\u63A8\u9001\u4E1A\u52A1\u9519\u8BEF " + (result.code ?? "\u672A\u77E5") + "\uFF1A" + (result.message ?? "\u8FD4\u56DE\u683C\u5F0F\u5F02\u5E38"));
      event.pushedAt = (/* @__PURE__ */ new Date()).toISOString();
      event.pushStatus = "sent";
      event.nextPushAt = null;
      event.pushLastError = null;
      status = "ok";
    } catch (error) {
      message = String(error).replace(/https?:\/\/[^\s"']+/g, "[\u63A8\u9001\u5730\u5740]");
      this.deferPush(event, message);
    } finally {
      const createdAt = (/* @__PURE__ */ new Date()).toISOString();
      this.state.pushLogs.unshift({ id: createdAt + ":" + crypto.randomUUID(), eventId: event.id, status, title, message, createdAt });
      this.state.pushLogs = this.state.pushLogs.slice(0, 120);
      this.addActivityLog({
        level: status === "ok" ? "success" : "error",
        type: "push_result",
        exchange: event.exchange,
        symbol: event.symbol,
        message: event.exchangeName + " " + event.symbol + (status === "ok" ? " \u63A8\u9001\u670D\u52A1\u5DF2\u63A5\u53D7\u3002" : " \u63A8\u9001\u5931\u8D25\uFF08" + event.pushAttempts + "/" + PUSH_RETRY_LIMIT + "\uFF09\uFF1A" + message + "\uFF1B" + ((event.pushAttempts ?? 0) >= PUSH_RETRY_LIMIT ? "\u91CD\u8BD5\u5DF2\u8FBE\u4E0A\u9650\uFF0C\u8BF7\u68C0\u67E5\u901A\u9053" : "\u5DF2\u5B89\u6392\u81EA\u52A8\u91CD\u8BD5") + "\u3002")
      });
      try {
        await this.persist();
        try {
          await this.settleGlobalPush(pushKey, claim.token, event.id, status === "ok");
        } catch {
          this.addActivityLog({ level: "error", type: "push_lock_error", exchange: event.exchange, symbol: event.symbol, message: event.exchangeName + " " + event.symbol + " \u63A8\u9001\u9501\u7ED3\u7B97\u5931\u8D25\uFF0C\u5DF2\u4FDD\u7559\u53D1\u9001\u7ED3\u679C\u3002" });
        }
      } finally {
        this.pushInFlight.delete(pushKey);
      }
    }
  }
  async retryPendingPushes() {
    const due = this.state.events.filter((row) => row.tradableAt && !row.pushedAt && row.pushStatus === "pending" && (!row.nextPushAt || Date.parse(row.nextPushAt) <= Date.now())).slice(0, 10);
    await Promise.all(due.map((event) => this.pushTradable(event)));
    return due.length > 0;
  }
  activateAnnouncedMarkets() {
    const now = Date.now();
    let changed = false;
    for (const row of this.state.announcements) {
      const scheduled = Date.parse(row.scheduledAt ?? "");
      if (row.action !== "listing" || row.marketType === "unknown" || !row.symbol || !Number.isFinite(scheduled) || scheduled > now || scheduled < now - 864e5) continue;
      const marketSymbol = row.exchange === "gate" ? row.symbol + "_USDT" : row.exchange === "okx" ? row.symbol + (row.marketType === "spot" ? "-USDT" : "-USDT-SWAP") : row.symbol + "USDT";
      const existing = this.state.events.some((event2) => event2.exchange === row.exchange && event2.marketType === row.marketType && event2.marketSymbol === marketSymbol);
      if (existing) continue;
      this.createMonitorEvent(row.exchange, { symbol: row.symbol, marketSymbol, scheduledAt: row.scheduledAt, marketStatus: "announcement_only", assetType: row.assetType }, row.fetchedAt, (/* @__PURE__ */ new Date()).toISOString(), row.marketType);
      changed = true;
      const event = this.state.events[0];
      if (scheduled < Date.parse(this.state.repairBaselineAt ?? "") || now - scheduled > HISTORICAL_NOTIFICATION_AGE_MS) {
        event.notificationPolicy = "historical_only";
        event.recoveredAfterGap = true;
      }
    }
    return changed;
  }
  async verifyActiveBooks() {
    const nowMs = Date.now();
    const active = this.state.events.filter(
      (row) => ["verifying", "waiting_book"].includes(row.monitorStatus) && (!row.scheduledAt || Date.parse(row.scheduledAt) <= nowMs) && (!row.nextBookCheckAt || Date.parse(row.nextBookCheckAt) <= nowMs)
    ).sort((a, b) => Date.parse(a.nextBookCheckAt ?? a.createdAt) - Date.parse(b.nextBookCheckAt ?? b.createdAt)).slice(0, 30);
    let changed = false;
    await Promise.all(active.map(async (event) => {
      const policy = classifyBookMonitor(event, nowMs);
      if (policy.phase === "retire") {
        this.retireBookMonitor(event, policy.reason, nowMs);
        changed = true;
        return;
      }
      event.monitorStatus = policy.phase === "verify" ? "verifying" : "waiting_book";
      event.nextBookCheckAt = new Date(nowMs + (policy.phase === "verify" ? BOOK_INTERVAL_MS : BOOK_RECHECK_MS)).toISOString();
      let book = null;
      try {
        book = await this.fetchRemoteBook(event.exchange, event.marketSymbol, event.marketType);
        event.lastBookError = null;
      } catch (error) {
        event.lastBookError = String(error).slice(0, 300);
      }
      event.bookChecks += 1;
      event.updatedAt = (/* @__PURE__ */ new Date()).toISOString();
      changed = true;
      if (!book || !Number.isFinite(book.bid) || !Number.isFinite(book.ask) || book.bid <= 0 || book.ask < book.bid) {
        if (event.monitorStatus === "waiting_book") event.marketMessage = event.lastBookError ? "\u76D8\u53E3\u6765\u6E90\u6682\u4E0D\u53EF\u7528\uFF0C\u7EE7\u7EED\u6BCF 30 \u79D2\u590D\u6838" : "\u6682\u672A\u53D6\u5F97\u6709\u6548\u76D8\u53E3\uFF0C\u7EE7\u7EED\u6BCF 30 \u79D2\u590D\u6838";
        return;
      }
      const match = this.findMatchingAnnouncement(event.exchange, event.symbol, event.scheduledAt, event.marketType);
      const checkedAt = (/* @__PURE__ */ new Date()).toISOString();
      event.tradableAt = checkedAt;
      event.firstBid = book.bid;
      event.firstAsk = book.ask;
      event.monitorStatus = "tradable";
      event.announcementMatched = Boolean(match);
      event.announcementTitle = match?.title ?? null;
      event.announcementUrl = match?.url ?? null;
      event.publishedAt = match?.publishedAt ?? null;
      event.marketMessage = match ? "\u5E02\u573A\u5DF2\u5F00\u653E\uFF0C\u516C\u544A\u5DF2\u5339\u914D" : "\u5E02\u573A\u5DF2\u5F00\u653E\uFF0C\u672A\u5339\u914D\u516C\u544A";
      event.updatedAt = checkedAt;
      event.pushStatus = event.notificationPolicy === "historical_only" ? "suppressed" : "pending";
      this.addActivityLog({
        level: "success",
        type: "market_opened",
        exchange: event.exchange,
        symbol: event.symbol,
        message: event.exchangeName + " " + event.marketSymbol + (event.recoveredAfterGap ? " \u8865\u67E5\u786E\u8BA4\u5F53\u524D\u53EF\u4EA4\u6613\uFF1B\u5386\u53F2\u9996\u6B21\u5F00\u76D8\u65F6\u95F4\u672A\u77E5\uFF0C\u4E0D\u8865\u63A8\u65E7\u6D88\u606F\u3002" : " \u5DF2\u53D6\u5F97\u9996\u6B21\u6709\u6548\u76D8\u53E3\uFF0C\u7ACB\u5373\u8FDB\u5165\u63A8\u9001\u961F\u5217\u3002")
      });
      await this.persist();
      await this.pushTradable(event);
    }));
    return changed;
  }
  retireBookMonitor(event, reason, nowMs = Date.now()) {
    event.monitorStatus = "expired_no_book";
    event.nextBookCheckAt = null;
    event.updatedAt = new Date(nowMs).toISOString();
    event.marketMessage = reason === "historical" ? "\u5386\u53F2\u8865\u67E5\u672A\u53D6\u5F97\u6709\u6548\u76D8\u53E3\uFF0C\u590D\u6838\u5DF2\u7ED3\u675F" : reason === "terminal" ? "\u4EA4\u6613\u6240\u8FD4\u56DE\u6807\u7684\u65E0\u6548\u6216\u5DF2\u7ED3\u675F\uFF0C\u76D8\u53E3\u590D\u6838\u5DF2\u505C\u6B62" : reason === "invalid_time" ? "\u76D8\u53E3\u590D\u6838\u65F6\u95F4\u5F02\u5E38\uFF0C\u4EFB\u52A1\u5DF2\u505C\u6B62" : "2 \u5C0F\u65F6\u5185\u672A\u53D6\u5F97\u6709\u6548\u76D8\u53E3\uFF0C\u590D\u6838\u5DF2\u7ED3\u675F";
  }
  hasActiveMonitors() {
    const now = Date.now();
    return this.state.events.some((row) => row.monitorStatus === "verifying" && Date.parse(row.monitorEndsAt) > now);
  }
  async scheduleNext(cycleStartedAt) {
    if (!this.state.running) {
      await this.ctx.storage.deleteAlarm();
      this.runtime.nextAlarmAt = null;
      return 0;
    }
    const delay = this.hasActiveMonitors() ? BOOK_INTERVAL_MS : INVENTORY_INTERVAL_MS;
    const at = Math.max(Date.now() + 100, cycleStartedAt + delay);
    await this.ctx.storage.setAlarm(at);
    this.runtime.nextAlarmAt = new Date(at).toISOString();
    return Math.max(100, at - Date.now());
  }
  async runCycle(forceAll = false) {
    if (this.cyclePromise) return await this.cyclePromise;
    const cycle = this.executeCycle(forceAll);
    this.cyclePromise = cycle;
    try {
      return await cycle;
    } finally {
      if (this.cyclePromise === cycle) this.cyclePromise = null;
    }
  }
  async executeCycle(forceAll) {
    if (!this.state.running || !await executionAllowed(this.env)) return { status: "standby" };
    const cycleStartedAt = Date.now();
    const recoveredLongGap = cycleStartedAt - Date.parse(this.runtime.lastCycleAt ?? "") > HISTORICAL_NOTIFICATION_AGE_MS;
    const lastNews = Date.parse(this.runtime.lastNewsScanAt ?? "");
    const lastContracts = Date.parse(this.runtime.lastContractScanAt ?? "");
    const scans = [];
    if (!this.newsPromise && (forceAll || !Number.isFinite(lastNews) || cycleStartedAt - lastNews >= NEWS_INTERVAL_MS - 500)) {
      this.newsPromise = this.scanNews(new Date(cycleStartedAt).toISOString()).then(async () => {
        await this.persist();
      }).catch((error) => {
        this.addActivityLog({ level: "error", type: "news_scan_error", message: `\u516C\u544A\u626B\u63CF\u5F02\u5E38\uFF1A${String(error)}` });
      }).finally(() => {
        this.newsPromise = null;
      });
      this.ctx.waitUntil(this.newsPromise);
    }
    if (forceAll || !Number.isFinite(lastContracts) || cycleStartedAt - lastContracts >= INVENTORY_INTERVAL_MS - 500) scans.push(this.scanContracts());
    const scanChanges = await Promise.all(scans);
    const activated = this.activateAnnouncedMarkets();
    let historicalRepair = false;
    if (recoveredLongGap) {
      for (const event of this.state.events) {
        const expectedAt = Date.parse(event.scheduledAt ?? event.firstDetectedAt);
        if (!event.tradableAt && expectedAt < cycleStartedAt - HISTORICAL_NOTIFICATION_AGE_MS && event.notificationPolicy !== "historical_only") {
          event.notificationPolicy = "historical_only";
          event.recoveredAfterGap = true;
          historicalRepair = true;
        }
      }
      this.addActivityLog({
        level: "warning",
        type: "monitor_gap_recovered",
        message: "\u76D1\u63A7\u4ECE\u957F\u65F6\u95F4\u4E2D\u65AD\u6062\u590D\uFF1B\u4E2D\u65AD\u671F\u95F4\u7684\u5386\u53F2\u5E02\u573A\u4EC5\u8865\u67E5\u8BB0\u5F55\uFF0C\u4E0D\u8865\u53D1\u65E7\u4E0A\u5E01\u901A\u77E5\u3002"
      });
    }
    await this.verifyActiveBooks();
    const pushChanged = await this.retryPendingPushes();
    this.runtime.lastCycleAt = (/* @__PURE__ */ new Date()).toISOString();
    const nextDelayMs = await this.scheduleNext(cycleStartedAt);
    if (activated || historicalRepair || recoveredLongGap || scanChanges.some(Boolean) || pushChanged || Date.now() - this.lastCheckpointAt >= 6e4) await this.persist();
    return { status: "ok", nextDelayMs };
  }
  async alarm() {
    if (!await executionAllowed(this.env)) return;
    if (!this.isPrimaryMonitor()) {
      await this.ctx.storage.deleteAlarm();
      console.warn(JSON.stringify({
        event: "exchange_monitor_instance_retired",
        durableObjectId: this.ctx.id.toString(),
        primaryDurableObjectId: this.env.MONITOR.idFromName(PRIMARY_MONITOR_NAME).toString(),
        at: (/* @__PURE__ */ new Date()).toISOString()
      }));
      return;
    }
    if (this.alarmBusy || !this.state.running) return;
    this.alarmBusy = true;
    try {
      const previousError = this.runtime.lastError;
      const outcome = await this.runCycle(false);
      this.runtime.lastError = null;
      if (previousError) {
        this.addActivityLog({ level: "success", type: "monitor_recovered", message: "\u76D1\u63A7\u5FAA\u73AF\u5DF2\u4ECE\u5F02\u5E38\u4E2D\u6062\u590D\u3002" });
        await this.persist();
      }
      console.log(JSON.stringify({
        event: "exchange_monitor_cycle_complete",
        at: (/* @__PURE__ */ new Date()).toISOString(),
        nextDelayMs: outcome.nextDelayMs,
        sourceCount: Array.from(this.sources.values()).filter((row) => row.kind === "contracts").length
      }));
    } catch (error) {
      const previousError = this.runtime.lastError;
      this.runtime.lastError = String(error);
      if (!previousError) {
        this.addActivityLog({ level: "error", type: "monitor_error", message: `\u76D1\u63A7\u5FAA\u73AF\u5F02\u5E38\uFF1A${String(error)}` });
      }
      const retryAt = Date.now() + 5e3;
      await this.ctx.storage.setAlarm(retryAt);
      this.runtime.nextAlarmAt = new Date(retryAt).toISOString();
      await this.persist();
    } finally {
      this.alarmBusy = false;
    }
  }
  snapshot() {
    const sources = Array.from(this.sources.values()).sort((a, b) => (a.exchange + ":" + a.kind).localeCompare(b.exchange + ":" + b.kind));
    const now = Date.now();
    const today = beijingDateKey(now);
    const healthy = sources.filter((row) => row.kind === "contracts").length === EXCHANGES.length && sources.filter((row) => row.kind === "news").length === Object.keys(NEWS_FETCHERS).length + 1 && sources.every((row) => ["ok", "market_only"].includes(row.status) && !row.consecutiveFailures && now - Date.parse(row.lastCheckedAt) < (row.kind === "news" ? 15e4 : 6e4)) && !this.runtime.lastError && now - Date.parse(this.runtime.lastCycleAt ?? "") < 6e4;
    const listingReminders = [];
    for (const exchange of EXCHANGES) {
      for (const [marketSymbol, item] of Object.entries(this.state.inventory[exchange.code])) {
        const scheduledAt = item.scheduledAt ?? null;
        const scheduled = Date.parse(scheduledAt ?? "");
        const event = this.state.events.find((row) => row.exchange === exchange.code && row.marketSymbol === marketSymbol && row.marketType === "contract");
        const visible = Number.isFinite(scheduled) ? scheduled > now || beijingDateKey(scheduled) === today : Boolean(event && beijingDateKey(event.firstDetectedAt) === today && event.notificationPolicy !== "historical_only");
        if (!visible) continue;
        if (scheduled - now > 30 * 864e5 && item.marketStatus === "PENDING_TRADING") continue;
        const announcement = this.findMatchingAnnouncement(exchange.code, item.symbol, scheduledAt);
        const assetType = mergedAssetType(item.assetType, announcement?.assetType, knownAnnouncementAssetType(this.state.announcements, item.symbol));
        listingReminders.push({
          id: exchange.code + ":" + marketSymbol + ":" + (scheduledAt ?? item.firstSeenAt),
          exchange: exchange.code,
          exchangeName: exchange.name,
          symbol: item.symbol,
          marketSymbol,
          marketType: "contract",
          contractKind: item.contractKind ?? "perpetual",
          scheduledAt,
          hasOccurred: Boolean(event?.tradableAt),
          marketStatus: item.marketStatus ?? null,
          assetType,
          assetLabel: assetType === "stock" ? "\u80A1\u7968" : null,
          firstDetectedAt: item.firstSeenAt,
          tradableAt: event?.tradableAt ?? null,
          recoveredAfterGap: Boolean(event?.recoveredAfterGap),
          announcementMatched: Boolean(announcement),
          announcementTitle: chineseAnnouncementTitle(announcement, item.symbol),
          announcementUrl: announcement?.url ?? null,
          publishedAt: announcement?.publishedAt ?? null
        });
      }
    }
    for (const announcement of this.state.announcements) {
      if (!["listing", "delisting"].includes(announcement.action) || announcement.marketType === "unknown") continue;
      const symbols = announcement.symbols?.length ? announcement.symbols : announcement.symbol ? [announcement.symbol] : [];
      const scheduled = Date.parse(announcement.scheduledAt ?? "");
      const visible = Number.isFinite(scheduled) ? scheduled > now || beijingDateKey(scheduled) === today : Boolean(announcement.publishedAt && beijingDateKey(announcement.publishedAt) === today);
      if (!visible) continue;
      for (const symbol of symbols) {
        if (listingReminders.some((item) => (item.action ?? "listing") === announcement.action && item.exchange === announcement.exchange && item.symbol === symbol && item.marketType === announcement.marketType)) continue;
        const event = announcement.action === "listing" ? this.state.events.find((row) => row.exchange === announcement.exchange && row.symbol === symbol && row.marketType === announcement.marketType) : void 0;
        const marketSymbol = announcement.exchange === "gate" ? symbol + "_USDT" : announcement.exchange === "okx" ? symbol + (announcement.marketType === "spot" ? "-USDT" : "-USDT-SWAP") : symbol + "USDT";
        const assetType = mergedAssetType(announcement.assetType, knownAnnouncementAssetType(this.state.announcements, symbol));
        listingReminders.push({
          id: announcement.exchange + ":announcement:" + announcement.marketType + ":" + symbol + ":" + announcement.key,
          action: announcement.action,
          openingSuspendsAt: announcement.openingSuspendsAt ?? null,
          exchange: announcement.exchange,
          exchangeName: announcement.exchangeName,
          symbol,
          marketSymbol,
          marketType: announcement.marketType,
          scheduledAt: announcement.scheduledAt,
          hasOccurred: Boolean(event?.tradableAt),
          marketStatus: "announcement_only",
          assetType,
          assetLabel: assetType === "stock" ? "\u80A1\u7968" : null,
          firstDetectedAt: announcement.fetchedAt,
          tradableAt: event?.tradableAt ?? null,
          recoveredAfterGap: Boolean(event?.recoveredAfterGap),
          announcementMatched: true,
          announcementTitle: chineseAnnouncementTitle(announcement, symbol),
          announcementUrl: announcement.url,
          publishedAt: announcement.publishedAt
        });
      }
    }
    listingReminders.sort((a, b) => Date.parse(a.scheduledAt ?? a.firstDetectedAt) - Date.parse(b.scheduledAt ?? b.firstDetectedAt));
    return {
      status: this.state.running ? healthy ? "running" : "degraded" : "stopped",
      updatedAt: (/* @__PURE__ */ new Date()).toISOString(),
      revision: "delisting-reminders-2026-09-06-r2",
      schedule: {
        inventoryIntervalSeconds: 15,
        newsIntervalSeconds: 60,
        newContractBookIntervalSeconds: 1,
        newContractBookDurationMinutes: 5,
        subsequentBookIntervalSeconds: 30
      },
      runtime: {
        ...this.runtime,
        nextNewsScanAt: this.runtime.lastNewsScanAt ? new Date(Date.parse(this.runtime.lastNewsScanAt) + NEWS_INTERVAL_MS).toISOString() : null,
        durableObjectId: this.ctx.id.toString(),
        primaryInstance: this.isPrimaryMonitor(),
        activeBookMonitors: this.state.events.filter((row) => ["verifying", "waiting_book"].includes(row.monitorStatus)).length,
        pendingPushes: this.state.events.filter((row) => row.pushStatus === "pending").length,
        failedPushes: this.state.events.filter((row) => row.pushStatus === "failed").length
      },
      sources,
      events: this.state.events.slice(0, 120).map(withoutRetiredCardFields),
      listingReminders,
      announcements: this.state.announcements.slice(0, 100).map((row) => ({
        id: row.key,
        exchange: row.exchange,
        exchangeName: row.exchangeName,
        symbol: row.symbol,
        symbols: row.symbols,
        title: row.title,
        displayTitle: chineseAnnouncementTitle(row, row.symbol ?? "\u672A\u8BC6\u522B\u6807\u7684"),
        url: row.url,
        publishedAt: row.publishedAt,
        scheduledAt: row.scheduledAt ?? null,
        action: row.action,
        openingSuspendsAt: row.openingSuspendsAt ?? null,
        marketType: row.marketType,
        assetType: row.assetType,
        fetchedAt: row.fetchedAt,
        detailStatus: row.detailStatus ?? (row.detailCheckedAt ? "parsed" : "pending"),
        detailCheckedAt: row.detailCheckedAt ?? null,
        detailError: row.detailError ?? null,
        detailSource: row.detailSource ?? null,
        discoveryDelayMs: row.publishedAt ? Math.max(0, Date.parse(row.fetchedAt) - Date.parse(row.publishedAt)) : null
      })),
      pushLogs: this.state.pushLogs.slice(0, 50),
      newsReadLogs: (this.state.newsReadLogs ?? []).slice(0, 120),
      newsHourlyStats: (this.state.newsHourlyStats ?? []).map((row) => ({ ...row, averageDurationMs: row.scans ? Math.round(row.totalDurationMs / row.scans) : 0 })),
      activityLogs: (this.state.activityLogs ?? []).filter((log) => log.type !== "book_verification_expired").slice(0, 50)
    };
  }
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/internal/readiness" && request.method === "GET") {
      const checks = await Promise.all([...EXCHANGES, { code: "bnus", name: "Binance.US" }].flatMap(({ code }) => {
        const contracts = code === "bnus" ? null : (async () => {
          try {
            const rows = await this.fetchRemoteContracts(code);
            const diagnostic = this.contractDiagnostics.get(code);
            const ok = rows.length > 0 && !diagnostic?.partial && diagnostic?.cacheStatus === "fresh";
            return { exchange: code, kind: "contracts", ok, error: ok ? null : "\u5408\u7EA6\u5217\u8868\u4E3A\u7A7A\u3001\u7F13\u5B58\u56DE\u9000\u6216\u8986\u76D6\u4E0D\u5B8C\u6574" };
          } catch (error) {
            return { exchange: code, kind: "contracts", ok: false, error: String(error) };
          }
        })();
        if (code === "hl") return contracts ? [contracts] : [];
        const news = (async () => {
          try {
            const result = await this.fetchRemoteNews(code);
            const blocked = result.rows.some((row) => row.detailStatus === "unavailable" && /403|429/.test(row.detailError ?? ""));
            const ok = !result.error && result.rows.length > 0 && !blocked;
            return { exchange: code, kind: "news", ok, error: result.error || (blocked ? "\u516C\u544A\u6B63\u6587\u8BF7\u6C42\u88AB\u62D2\u7EDD\u6216\u9650\u6D41" : !result.rows.length ? "\u516C\u544A\u76EE\u5F55\u4E3A\u7A7A" : null) };
          } catch (error) {
            return { exchange: code, kind: "news", ok: false, error: String(error) };
          }
        })();
        return contracts ? [contracts, news] : [news];
      }));
      const quotaFailure = checks.find((row) => !row.ok && isDailyPlatformQuota(row.error));
      if (quotaFailure) return serviceUnavailable(quotaFailure.error);
      return json({ healthy: checks.every((row) => row.ok), checkedAt: (/* @__PURE__ */ new Date()).toISOString(), checks });
    }
    if (url.pathname === "/internal/push/claim" && request.method === "POST") {
      if (!this.isPrimaryMonitor()) return json({ error: "not_primary" }, 409);
      const body = await request.json();
      if (!body.pushKey || !body.eventId) return json({ error: "invalid_request" }, 400);
      return json(await this.claimLocalPush(body.pushKey, body.eventId));
    }
    if (url.pathname === "/internal/push/settle" && request.method === "POST") {
      if (!this.isPrimaryMonitor()) return json({ error: "not_primary" }, 409);
      const body = await request.json();
      if (!body.pushKey || !body.token || !body.eventId || typeof body.sent !== "boolean") {
        return json({ error: "invalid_request" }, 400);
      }
      await this.settleLocalPush(body.pushKey, body.token, body.eventId, body.sent);
      return json({ status: "ok" });
    }
    if (url.pathname === "/proxy/contracts") {
      const exchange = url.searchParams.get("exchange");
      if (!exchange || !EXCHANGES.some((row) => row.code === exchange)) return json({ error: "invalid_exchange" }, 400);
      const cached = this.contractCache.get(exchange);
      const backoffUntil = this.contractBackoffUntil.get(exchange) ?? 0;
      if (cached && Date.now() < backoffUntil) {
        return json({
          contracts: cached.contracts,
          cacheStatus: "backoff",
          cachedAt: cached.fetchedAt,
          retryAt: new Date(backoffUntil).toISOString()
        });
      }
      try {
        const contracts = await fetchContracts(exchange);
        const fetchedAt = (/* @__PURE__ */ new Date()).toISOString();
        this.contractCache.set(exchange, { contracts, fetchedAt });
        this.contractBackoffUntil.delete(exchange);
        this.contractFailureCounts.delete(exchange);
        return json({ contracts, cacheStatus: "fresh", cachedAt: fetchedAt });
      } catch (error) {
        const failures = (this.contractFailureCounts.get(exchange) ?? 0) + 1;
        this.contractFailureCounts.set(exchange, failures);
        const rateLimited = /\b429\b|too many requests|rate.?limit/i.test(String(error));
        const backoffMs = Math.min(6e4, (rateLimited ? 1e4 : 5e3) * 2 ** Math.min(failures - 1, 3));
        this.contractBackoffUntil.set(exchange, Date.now() + backoffMs);
        if (cached && Date.now() - Date.parse(cached.fetchedAt) <= 5 * 6e4) {
          console.warn(JSON.stringify({
            event: "exchange_contract_source_cached_fallback",
            exchange,
            error: String(error),
            cachedAt: cached.fetchedAt,
            retryAt: new Date(Date.now() + backoffMs).toISOString()
          }));
          return json({
            contracts: cached.contracts,
            cacheStatus: "fallback",
            cachedAt: cached.fetchedAt,
            retryAt: new Date(Date.now() + backoffMs).toISOString(),
            warning: String(error)
          });
        }
        return json({ error: String(error) }, 502);
      }
    }
    if (url.pathname === "/proxy/news") {
      const exchange = url.searchParams.get("exchange");
      const fetcher = exchange ? NEWS_FETCHERS[exchange] : null;
      if (!exchange || !fetcher) return json({ error: "invalid_exchange" }, 400);
      const trace = { attempts: [] };
      try {
        const catalogueByUrl = /* @__PURE__ */ new Map();
        for (const row of deduplicateCatalogue(await fetcher(trace))) {
          const before = catalogueByUrl.get(row.url);
          catalogueByUrl.set(row.url, before ? { ...before, symbols: [...new Set([...before.symbols ?? [], ...row.symbols ?? [], before.symbol, row.symbol].filter((value) => Boolean(value)))] } : row);
        }
        const catalogue = [...catalogueByUrl.values()];
        const now = Date.now();
        const attemptsKey = "news-detail-attempts:v2:" + exchange;
        const savedAttempts = await this.ctx.storage.get(attemptsKey) ?? {};
        for (const [key, at] of Object.entries(savedAttempts)) this.newsDetailAttempts.set(key, Math.max(at, this.newsDetailAttempts.get(key) ?? 0));
        const cacheKeys = new Map(await Promise.all([...new Set(catalogue.map((row) => row.url))].map(async (url2) => {
          const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(url2));
          return [url2, "news-detail:v3:" + [...new Uint8Array(digest)].map((n) => n.toString(16).padStart(2, "0")).join("")];
        })));
        const missing = [...cacheKeys].filter(([url2]) => !this.newsDetailCache.has(url2));
        for (let offset = 0; offset < missing.length; offset += 100) {
          const batch = missing.slice(offset, offset + 100);
          const saved = await this.ctx.storage.get(batch.map(([, key]) => key));
          for (const [url2, key] of batch) this.newsDetailCache.set(url2, saved.get(key) ?? { rows: [], expiresAt: now });
        }
        for (const base of catalogue) {
          const cached = this.newsDetailCache.get(base.url);
          if (!cached?.rows.length || cached.rows.every((row) => row.detailParserVersion === 3 || row.detailParserVersion === 2 && (row.action === "delisting" || row.exchange === "aster"))) continue;
          if (cached.rows.some((row) => row.detailStatus === "unavailable")) continue;
          const text = cached.rows[0].detailText;
          if (exchange === "aster" || !text || /Loading\.\.\./.test(text) && text.trim().length < 500) {
            cached.expiresAt = Math.min(cached.expiresAt, now);
            continue;
          }
          const rows2 = listingRowsFromText({ ...base, publishedAt: cached.rows[0].publishedAt ?? base.publishedAt }, text);
          const updated = { rows: rows2, expiresAt: cached.expiresAt };
          if (JSON.stringify(updated).length < 12e4) await this.ctx.storage.put(cacheKeys.get(base.url), updated);
          this.newsDetailCache.set(base.url, updated);
        }
        const eligible = catalogue.filter((base) => (base.action === "listing" && base.marketType !== "unknown" || base.action === "delisting") && (this.newsDetailCache.get(base.url)?.expiresAt ?? 0) <= now);
        eligible.sort((a, b) => Number(Date.parse(b.publishedAt ?? b.sourceUpdatedAt ?? "") >= now - 2 * 864e5) - Number(Date.parse(a.publishedAt ?? a.sourceUpdatedAt ?? "") >= now - 2 * 864e5) || (this.newsDetailAttempts.get(a.url) ?? 0) - (this.newsDetailAttempts.get(b.url) ?? 0) || Number(b.action === "delisting") - Number(a.action === "delisting") || Date.parse(b.publishedAt ?? b.sourceUpdatedAt ?? "") - Date.parse(a.publishedAt ?? a.sourceUpdatedAt ?? "") || Number(Boolean(a.symbol)) - Number(Boolean(b.symbol)));
        const selectedUrls = new Set(eligible.slice(0, exchange === "aster" ? 3 : 1).map((row) => row.url));
        for (const url2 of selectedUrls) this.newsDetailAttempts.set(url2, now);
        let detailFailures = 0;
        const rows = await Promise.all(catalogue.map(async (base) => {
          if (base.action !== "delisting" && (base.action !== "listing" || base.marketType === "unknown")) return [base];
          const cached = this.newsDetailCache.get(base.url);
          if (cached && cached.expiresAt > now) return cached.rows;
          if (!selectedUrls.has(base.url)) return [{ ...base, detailStatus: "pending" }];
          try {
            const detailed = await fetchAnnouncementDetail(base, trace);
            const compact = detailed.map((row) => ({ ...row, detailText: row.detailText?.slice(0, 8e3) }));
            const expiry = base.publishedAt && Date.parse(base.publishedAt) < now - 864e5 ? 864e5 : 30 * 6e4;
            const entry = { rows: compact, expiresAt: now + expiry };
            if (new TextEncoder().encode(JSON.stringify(entry)).length < 12e4) await this.ctx.storage.put(cacheKeys.get(base.url), entry);
            this.newsDetailCache.set(base.url, entry);
            return compact;
          } catch (error) {
            detailFailures += 1;
            const unavailable = { ...base, detailStatus: "unavailable", detailError: String(error).slice(0, 500) };
            const entry = { rows: [unavailable], expiresAt: now + (/403|429/.test(String(error)) ? 15 : 5) * 6e4 };
            await this.ctx.storage.put(cacheKeys.get(base.url), entry);
            this.newsDetailCache.set(base.url, entry);
            console.warn(JSON.stringify({ event: "announcement_detail_unavailable", exchange, url: base.url, error: String(error) }));
            return [unavailable];
          }
        }));
        for (const [key, entry] of this.newsDetailCache) if (entry.expiresAt < now - 864e5) this.newsDetailCache.delete(key);
        const currentUrls = new Set(catalogue.map((row) => row.url));
        await this.ctx.storage.put(attemptsKey, Object.fromEntries([...this.newsDetailAttempts].filter(([key]) => currentUrls.has(key))));
        const announcements = rows.flat();
        const warning = detailProgressMessage(announcements);
        return json({ announcements, httpAttempts: trace.attempts, warning });
      } catch (error) {
        return json({ error: String(error), httpAttempts: trace.attempts }, 502);
      }
    }
    if (url.pathname === "/proxy/book") {
      const exchange = url.searchParams.get("exchange");
      const marketSymbol = url.searchParams.get("marketSymbol");
      if (!exchange || !marketSymbol || !(EXCHANGES.some((row) => row.code === exchange) || exchange === "bnus" && url.searchParams.get("marketType") === "spot")) return json({ error: "invalid_request" }, 400);
      try {
        const book = url.searchParams.get("marketType") === "spot" ? await fetchSpotBook(exchange, marketSymbol) : await fetchBook(exchange, marketSymbol);
        return json({ book, source: "official", checkedAt: (/* @__PURE__ */ new Date()).toISOString() });
      } catch (error) {
        return json({ error: String(error) }, 502);
      }
    }
    if (url.pathname === "/health" || url.pathname === "/snapshot") {
      if (isDailyPlatformQuota(this.runtime.lastError)) return serviceUnavailable(this.runtime.lastError);
      return json(this.snapshot());
    }
    if (url.pathname === "/admin/export" && request.method === "GET") {
      if (this.state.running || this.cyclePromise || this.newsPromise || this.pushInFlight.size) return json({ error: "stop_before_export" }, 409);
      await this.persist();
      const locks = Object.fromEntries(await this.ctx.storage.list({ prefix: "push-lock:" }));
      return json({ version: 1, state: this.state, locks });
    }
    if (url.pathname === "/admin/import" && request.method === "POST") {
      if (this.state.running || this.cyclePromise || this.newsPromise || this.pushInFlight.size) return json({ error: "stop_before_import" }, 409);
      const payload = await request.json();
      if (payload.version !== 1 || payload.state?.version !== 1 || !Array.isArray(payload.state.announcements) || !Array.isArray(payload.state.events) || !EXCHANGES.every(({ code }) => payload.state.inventory?.[code])) return json({ error: "invalid_backup" }, 400);
      const previous = this.state;
      this.state = { ...payload.state, running: false };
      this.sources = new Map((this.state.sources ?? []).map((row) => [`${row.exchange}:${row.kind}`, row]));
      this.runtime = { lastCycleAt: null, lastContractScanAt: null, lastNewsScanAt: null, nextAlarmAt: null, lastError: null };
      try {
        for (const [key, value] of Object.entries(payload.locks ?? {})) {
          if (!key.startsWith("push-lock:")) throw new Error("invalid_lock_key");
          await this.ctx.storage.put(key, value);
        }
        await this.persist();
      } catch (error) {
        this.state = previous;
        throw error;
      }
      return json({ status: "imported", running: false });
    }
    if (url.pathname === "/internal/heartbeat" && request.method === "POST") {
      if (!this.isPrimaryMonitor() || !this.state.running) return json({ status: "stopped" });
      const alarmAt = await this.ctx.storage.getAlarm();
      if (alarmAt === null || alarmAt < Date.now() - 6e4) {
        const at = Date.now() + 100;
        await this.ctx.storage.setAlarm(at);
        this.runtime.nextAlarmAt = new Date(at).toISOString();
      }
      return json({ status: "ok" });
    }
    if (url.pathname === "/admin/start" && request.method === "POST") {
      if (!await executionAllowed(this.env)) return json({ error: "execution_lease_required" }, 409);
      if (!this.isPrimaryMonitor()) {
        await this.ctx.storage.deleteAlarm();
        return json({ status: "retired_non_primary" }, 409);
      }
      if (!this.state.running) {
        this.state.running = true;
        this.addActivityLog({ level: "success", type: "monitor_started", message: "\u4EA4\u6613\u6240\u76D1\u63A7\u5DF2\u542F\u52A8\u3002" });
      }
      const at = Date.now() + 100;
      await this.ctx.storage.setAlarm(at);
      this.runtime.nextAlarmAt = new Date(at).toISOString();
      await this.persist();
      return json({ status: "starting" });
    }
    if (url.pathname === "/admin/scan" && request.method === "POST") {
      if (!this.state.running) return json({ error: "monitor_stopped" }, 409);
      return json(await this.runCycle(true));
    }
    if (url.pathname === "/admin/stop" && request.method === "POST") {
      if (this.state.running) {
        this.state.running = false;
        this.addActivityLog({ level: "warning", type: "monitor_stopped", message: "\u4EA4\u6613\u6240\u76D1\u63A7\u5DF2\u505C\u6B62\u3002" });
      }
      await this.ctx.storage.deleteAlarm();
      this.runtime.nextAlarmAt = null;
      await Promise.allSettled([this.cyclePromise, this.newsPromise].filter(Boolean));
      await this.ctx.storage.deleteAlarm();
      await this.persist();
      return json({ status: "stopped" });
    }
    return json({ error: "not_found" }, 404);
  }
};
function corsHeaders(request, env) {
  const origin = request.headers.get("origin");
  const allowed = env.PUBLIC_SITE_ORIGIN;
  return {
    "access-control-allow-origin": !allowed || !origin || origin === allowed ? origin ?? allowed ?? "*" : allowed,
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "access-control-allow-headers": "authorization,content-type",
    vary: "Origin"
  };
}
__name(corsHeaders, "corsHeaders");
function monitorStub(env) {
  const id = env.MONITOR.idFromName(PRIMARY_MONITOR_NAME);
  return env.MONITOR.get(id, { locationHint: "apac-se" });
}
__name(monitorStub, "monitorStub");
function isDailyPlatformQuota(error) {
  return /exceeded allowed.*rows (?:read|written)|durable objects.*(?:exceeded|exhausted).*(?:daily|per day)|exceeded.*durable objects.*(?:daily|per day)/i.test(String(error));
}
__name(isDailyPlatformQuota, "isDailyPlatformQuota");
function serviceUnavailable(error, headers = {}) {
  const quota = isDailyPlatformQuota(error);
  const now = Date.now();
  const retryAt = quota ? new Date(Math.floor(now / 864e5) * 864e5 + 864e5).toISOString() : null;
  const outgoing = new Headers(headers);
  outgoing.set("cache-control", "no-store");
  if (retryAt) outgoing.set("retry-after", String(Math.ceil((Date.parse(retryAt) - now) / 1e3)));
  return json({
    status: "unavailable",
    code: quota ? "STORAGE_DAILY_QUOTA_EXCEEDED" : "MONITOR_UNAVAILABLE",
    error: quota ? `\u4E91\u7AEF\u4ECA\u65E5\u514D\u8D39\u989D\u5EA6\u5DF2\u7528\u5C3D\uFF0C\u76D1\u63A7\u4E0E\u63A8\u9001\u6682\u4E0D\u53EF\u7528\uFF1B\u9884\u8BA1\u5317\u4EAC\u65F6\u95F4 ${displayBeijing(retryAt)} \u989D\u5EA6\u91CD\u7F6E\uFF0C\u5C4A\u65F6\u4ECD\u9700\u9A8C\u8BC1\u6062\u590D\u3002` : "\u76D1\u63A7\u670D\u52A1\u6682\u4E0D\u53EF\u7528\uFF0C\u5C1A\u672A\u8BFB\u53D6\u5230\u6700\u65B0\u6570\u636E\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u3002",
    retryAt,
    retrySource: quota ? "daily-reset-estimate" : null
  }, 503, outgoing);
}
__name(serviceUnavailable, "serviceUnavailable");
var index_default = {
  async fetch(request, env) {
    const headers = corsHeaders(request, env);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers });
    const url = new URL(request.url);
    if (url.pathname.startsWith("/control/")) {
      if (!env.CONTROL_TOKEN || request.headers.get("authorization") !== `Bearer ${env.CONTROL_TOKEN}`) return json({ error: "unauthorized" }, 401, headers);
      try {
        if (url.pathname === "/control/probe" && request.method === "GET") {
          return await monitorStub(env).fetch(new Request("https://monitor.internal/internal/readiness"));
        }
        if (url.pathname === "/control/backup" && request.method === "GET") {
          const backup = await env.STATE_KV.get(MONITOR_STATE_KEY);
          return new Response(backup, { status: backup ? 200 : 404, headers: { "content-type": "application/json", "cache-control": "no-store" } });
        }
        if (url.pathname === "/control/lease" && request.method === "POST") {
          const { active } = await request.json();
          if (typeof active !== "boolean") return json({ error: "invalid_active" }, 400);
          const until = active ? Date.now() + CLOUD_LEASE_MS : 0;
          await env.STATE_KV.put("execution-lease:v1", JSON.stringify({ until }));
          return json({ until, safeLocalStartAt: Date.now() + CLOUD_LEASE_MS + 2e4 });
        }
        if (/^\/control\/(start|stop|export|import)$/.test(url.pathname)) {
          return await monitorStub(env).fetch(new Request("https://monitor.internal/admin/" + url.pathname.split("/").pop(), request));
        }
        return json({ error: "not_found" }, 404);
      } catch (error) {
        return serviceUnavailable(error, headers);
      }
    }
    if (url.pathname.startsWith("/internal/")) return json({ error: "not_found" }, 404, headers);
    if ((url.pathname === "/health" || url.pathname === "/snapshot") && env.CONTROL_TOKEN && !await executionAllowed(env)) {
      return json({ status: "standby", code: "CLOUD_STANDBY", error: "\u4E91\u7AEF\u5904\u4E8E\u5907\u7528\u72B6\u6001\uFF0C\u4E0D\u91C7\u96C6\u3001\u4E0D\u63A8\u9001\u3002\u8BF7\u5728\u672C\u673A\u5957\u5229\u7CFB\u7EDF\u67E5\u770B\u65B0\u95FB\u6216\u5207\u6362\u8FD0\u884C\u4F4D\u7F6E\u3002" }, 503, headers);
    }
    const stub = monitorStub(env);
    if (url.pathname === "/robots.txt") {
      return new Response("User-agent: *\nDisallow: /admin/\n", {
        headers: { ...headers, "content-type": "text/plain; charset=utf-8" }
      });
    }
    const admin = url.pathname.startsWith("/admin/") || url.pathname.startsWith("/proxy/");
    if (admin && request.headers.get("authorization") !== `Bearer ${env.ADMIN_TOKEN}`) return json({ error: "unauthorized" }, 401, headers);
    const forwarded = new Request(`https://monitor.internal${url.pathname}${url.search}`, request);
    let response;
    try {
      response = await stub.fetch(forwarded);
    } catch (error) {
      console.error(JSON.stringify({ event: "monitor_request_failed", path: url.pathname, error: String(error) }));
      return serviceUnavailable(error, headers);
    }
    const outgoing = new Headers(response.headers);
    for (const [key, value] of Object.entries(headers)) outgoing.set(key, value);
    return new Response(response.body, { status: response.status, headers: outgoing });
  },
  async scheduled(_controller, env) {
    if (!await executionAllowed(env)) return;
    const stub = monitorStub(env);
    try {
      const response = await stub.fetch(new Request("https://monitor.internal/internal/heartbeat", { method: "POST" }));
      console.log(JSON.stringify({ event: "exchange_monitor_heartbeat", at: (/* @__PURE__ */ new Date()).toISOString(), status: response.status }));
    } catch (error) {
      console.error(JSON.stringify({ event: "monitor_heartbeat_failed", error: String(error) }));
    }
  }
};
export {
  ExchangeMonitor,
  index_default as default
};
//# sourceMappingURL=index.js.map
