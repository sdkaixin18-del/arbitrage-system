#!/usr/bin/env node
"use strict";

// Read-only executable-book relay for the Astro cloud host. It deliberately
// uses public market data only: no Astro credentials are loaded, no TCP port
// is opened, and it never calls Astro-core or Astro server control APIs.
// The local bridge sends the exact visible-card target set over stdin. Empty
// targets leave this process alive but close every exchange socket and perform
// zero exchange REST requests.
const readline = require("node:readline");
const zlib = require("node:zlib");

const PULSE_URLS = [
  "https://pulse-lite-api.astro-btc.xyz/api/query/new",
  "https://pulse-api.astro-btc.xyz/api/query/second",
];

function loadWebSocket() {
  for (const candidate of ["ws", "/home/ubuntu/astro-server/node_modules/ws"]) {
    try {
      return require(candidate).WebSocket || require(candidate);
    } catch (_) {
      // Try the next known installation location.
    }
  }
  throw new Error("ws module is unavailable");
}

const WebSocket = loadWebSocket();
const ALLOWED = {
  bn: new Set([
    "KSTRUSDT", "MINIMAXUSDT", "SPCXUSDT", "SNDKUSDT", "DRAMUSDT",
    "MUUSDT", "SKHYUSDT", "HOODUSDT", "INTCUSDT", "MSTRUSDT",
    "CXMTUSDT", "UNITREEUSDT",
  ]),
  bg: new Set(["SKHYNIXUSDT", "SKHYUSDT"]),
  gt: new Set(["BOT_USDT", "CXMT_USDT", "UNITREE_USDT", "KSTR_USDT"]),
  hl: new Set(["CXMT", "GIGADEV"]),
};
const VENUES = Object.keys(ALLOWED);
const desired = Object.fromEntries(VENUES.map((venue) => [venue, new Set()]));
const books = new Map();
const connections = new Map();
const controllers = new Map();
const sockets = new Set();
const startedAt = Date.now();
const marketRequests = { bn: 0, bg: 0, gt: 0, hl: 0 };
let targetGeneration = 0;
let targetUpdatedAt = null;
let restRefreshRunning = false;
let lastRestFailureLogAt = 0;
let lastMarketRequestAt = null;
let stopping = false;
let dexRuntime = null;
let dexQuoteTail = Promise.resolve();
let lastDexQuoteStartedAt = 0;

function decimalString(rawValue, decimalsValue) {
  const raw = String(rawValue ?? "").trim();
  const decimals = Number(decimalsValue);
  if (!/^\d+$/.test(raw) || !Number.isInteger(decimals) || decimals < 0 || decimals > 36) {
    throw new Error("invalid OKXDEX token amount");
  }
  const padded = raw.padStart(decimals + 1, "0");
  const whole = padded.slice(0, padded.length - decimals) || "0";
  const fraction = decimals ? padded.slice(-decimals).replace(/0+$/, "") : "";
  return fraction ? `${whole}.${fraction}` : whole;
}

async function loadDexRuntime() {
  if (dexRuntime) return dexRuntime;
  // Astro core is bytecode-only. Loading its read-only quote helper here keeps
  // the OKX Web3 credentials inside the cloud container; the bridge returns
  // only sanitized prices, amounts, fees and route metadata.
  require("/usr/lib/node_modules/bytenode");
  const utils = require("/home/ubuntu/astro-core/src/utils/index.jsc");
  const rest = require("/home/ubuntu/astro-core/src/utils/rest-api.jsc");
  dexRuntime = { utils, rest };
  return dexRuntime;
}

function enqueueDexQuote(task) {
  const run = async () => {
    // The official aggregator is rate limited. Serialize requests and leave a
    // small gap so several simultaneous Pulse candidates fail closed instead
    // of producing HTTP 50011 bursts.
    const waitMs = Math.max(0, 1_100 - (Date.now() - lastDexQuoteStartedAt));
    if (waitMs) await new Promise((resolve) => setTimeout(resolve, waitMs));
    lastDexQuoteStartedAt = Date.now();
    return task();
  };
  const result = dexQuoteTail.then(run, run);
  dexQuoteTail = result.catch(() => undefined);
  return result;
}

async function okxdexExecutableQuote(request) {
  if (request?.exchange === "pancakeswapv3") return pancakeswapExecutableQuote(request);
  if (request?.exchange && request.exchange !== "okxdex") throw new Error("unsupported DEX quote venue");
  const symbol = String(request?.symbol || "").trim().toUpperCase();
  const chainIndex = String(request?.chainIndex || "").trim();
  const contractAddress = String(request?.contractAddress || "").trim();
  const amountUsdt = Number(request?.amountUsdt);
  if (!/^[A-Z0-9\u4e00-\u9fff._-]{1,32}$/.test(symbol)) throw new Error("invalid OKXDEX symbol");
  if (!/^\d{1,8}$/.test(chainIndex)) throw new Error("invalid OKXDEX chainIndex");
  if (!contractAddress || contractAddress.length > 128) throw new Error("invalid OKXDEX contractAddress");
  if (!Number.isFinite(amountUsdt) || amountUsdt <= 0 || amountUsdt > 10_000) {
    throw new Error("invalid OKXDEX USDT amount");
  }

  return enqueueDexQuote(async () => {
    const { utils, rest } = await loadDexRuntime();
    const [keys, coins] = await Promise.all([utils.loadSafeKeys(), utils.loadDexCoins()]);
    const coin = (Array.isArray(coins) ? coins : []).find((item) => (
      String(item?.name || "").trim().toUpperCase() === symbol
      && String(item?.chainIndex || "").trim() === chainIndex
      && String(item?.contractAddress || "").trim().toLowerCase() === contractAddress.toLowerCase()
    ));
    if (!coin) throw new Error("OKXDEX coin is not configured in Astro with this exact chain and contract");
    const credentials = keys?.okxdex;
    if (!credentials?.K || !credentials?.S || !credentials?.P) {
      throw new Error("Astro OKXDEX quote credentials are unavailable");
    }
    // The fifth argument is the buy direction. This is quote-only: it calls
    // the official aggregator quote endpoint and never requests swap calldata.
    const quote = await rest.okxdexQuote(
      credentials.K,
      credentials.S,
      credentials.P,
      amountUsdt,
      true,
      coin,
    );
    if (!quote || !quote.raw) throw new Error("OKXDEX returned no executable route");
    const raw = quote.raw;
    const fromDecimals = Number(quote.fromDecimals ?? raw?.fromToken?.decimal);
    const toDecimals = Number(quote.toDecimals ?? raw?.toToken?.decimal);
    const fromRaw = String(raw.fromTokenAmount ?? quote.fromAmount ?? "");
    const toRaw = String(raw.toTokenAmount ?? quote.toAmount ?? "");
    const quotedAt = Date.now();
    return {
      source: "astro_core_okx_v6_quote",
      quotedAt,
      symbol,
      chainIndex,
      contractAddress: String(coin.contractAddress),
      amountUsdt,
      fromTokenAddress: String(quote.fromTokenAddress || raw?.fromToken?.tokenContractAddress || ""),
      toTokenAddress: String(quote.toTokenAddress || raw?.toToken?.tokenContractAddress || ""),
      fromDecimals,
      toDecimals,
      fromAmount: decimalString(fromRaw, fromDecimals),
      toAmount: decimalString(toRaw, toDecimals),
      tradeFeeUsd: String(raw.tradeFee ?? "0"),
      estimateGasFee: String(raw.estimateGasFee ?? ""),
      priceImpactPercent: raw.priceImpactPercent == null ? null : String(raw.priceImpactPercent),
      buyTaxRate: raw?.toToken?.taxRate == null ? null : String(raw.toToken.taxRate),
      quoteId: String(raw.quoteId || ""),
      routeDexes: [...new Set((raw.dexRouterList || []).map((item) => (
        String(item?.dexProtocol?.dexName || "").trim()
      )).filter(Boolean))],
    };
  });
}

async function pancakeswapExecutableQuote(request) {
  const {utils} = await loadDexRuntime();
  const {Contract, JsonRpcProvider, FetchRequest, parseUnits, formatUnits} = require("/home/ubuntu/astro-core/node_modules/ethers");
  const helper = require("/home/ubuntu/astro-core/src/trade/_pancakeswapv3_helper.jsc");
  const chain = String(request.chainIndex || "");
  const symbol = String(request.symbol || "").toUpperCase();
  const address = String(request.contractAddress || "").toLowerCase();
  const amount = Number(request.amountUsdt);
  if (!/^\d{1,8}$/.test(chain) || !/^0x[0-9a-f]{40}$/.test(address) || !(amount > 0 && amount <= 10000)) throw new Error("invalid PancakeSwap quote parameters");
  const coins = await utils.loadDexCoins();
  const coin = coins.find(c => String(c.name).toUpperCase() === symbol && String(c.chainIndex) === chain && String(c.contractAddress).toLowerCase() === address);
  if (!coin || coin.quote !== "USDT") throw new Error("PancakeSwap coin requires exact configured chain/address and USDT quote");
  const config = helper.getChainConfig(Number(chain));
  const quoteToken = helper.getQuoteTokenInfo(Number(chain), "USDT");
  if (!config?.quoterV2 || !config.rpcUrl || !quoteToken) throw new Error("PancakeSwap chain is unavailable");
  const rpc = new FetchRequest(config.rpcUrl);
  rpc.timeout = 4000;
  // Provider only: no wallet, approval, signing or broadcast methods are used.
  const provider = new JsonRpcProvider(rpc, Number(chain), {staticNetwork:true, batchMaxCount:1});
  try {
    const token = new Contract(address, ["function decimals() view returns (uint8)"], provider);
    const quoter = new Contract(config.quoterV2, ["function quoteExactInputSingle((address tokenIn,address tokenOut,uint256 amountIn,uint24 fee,uint160 sqrtPriceLimitX96) params) returns (uint256 amountOut,uint160 sqrtPriceX96After,uint32 initializedTicksCrossed,uint256 gasEstimate)"], provider);
    const [decimals, feeData, nativeResponse] = await Promise.all([
      token.decimals(), provider.getFeeData(),
      fetch(`https://api.binance.com/api/v3/ticker/price?symbol=${config.nativeCoin}USDT`, {signal:AbortSignal.timeout(4000)}),
    ]);
    if (!nativeResponse.ok) throw new Error("native token price unavailable");
    const nativePrice = Number((await nativeResponse.json()).price);
    if (!(nativePrice > 0) || feeData.gasPrice == null) throw new Error("gas cost unavailable");
    const input = parseUnits(String(amount), quoteToken.decimals);
    const settled = await Promise.allSettled([100,500,2500,10000].map(async fee => {
      const result = await quoter.quoteExactInputSingle.staticCall([quoteToken.address,address,input,fee,0]);
      return {fee, output:result[0], gas:result[3], quotedAt:Date.now()};
    }));
    const quotes = settled.filter(x=>x.status === "fulfilled" && x.value.output > 0n).map(x=>x.value);
    if (!quotes.length) throw new Error("PancakeSwap has no executable direct USDT V3 pool");
    quotes.sort((a,b)=>a.output>b.output?-1:a.output<b.output?1:0);
    const best = quotes[0];
    const gasUnits = best.gas + 150000n;
    const gasUsd = Number(formatUnits(gasUnits * feeData.gasPrice,18)) * nativePrice;
    return {source:"pancakeswap_v3_quoter_eth_call", exchange:"pancakeswapv3", quotedAt:best.quotedAt,
      symbol, chainIndex:chain, contractAddress:address, amountUsdt:amount,
      fromAmount:String(amount), toAmount:formatUnits(best.output,Number(decimals)), tradeFeeUsd:String(gasUsd),
      networkFeeEstimated:true, estimateGasFee:String(gasUnits), feeTier:best.fee,
      routeDexes:["PancakeSwap V3"], quoteId:"", fromTokenAddress:quoteToken.address, toTokenAddress:address};
  } finally { provider.destroy(); }
}

function iso(value) {
  return Number.isFinite(value) && value > 0 ? new Date(value).toISOString() : null;
}

function safeTimestamp(value, fallback) {
  const parsed = Number(value);
  const now = Date.now();
  return Number.isFinite(parsed) && parsed >= 1_577_836_800_000 && parsed <= now + 10_000
    ? parsed
    : fallback;
}

function log(message) {
  process.stderr.write(`[astro-quote-gateway] ${new Date().toISOString()} ${message}\n`);
}

function targetKey(venue, symbol) {
  return `${venue}:${String(symbol || "").toUpperCase()}`;
}

function targetList() {
  return VENUES.flatMap((venue) => [...desired[venue]].sort().map((symbol) => `${venue}:${symbol}`));
}

function parseTargets(rawTargets) {
  if (!Array.isArray(rawTargets)) throw new Error("targets must be an array");
  const next = Object.fromEntries(VENUES.map((venue) => [venue, new Set()]));
  for (const rawTarget of rawTargets) {
    const normalized = String(rawTarget || "").trim();
    const match = /^(bn|bg|gt|hl):([A-Z0-9_]+)$/.exec(normalized);
    if (!match || !ALLOWED[match[1]].has(match[2])) {
      throw new Error(`unsupported quote target: ${normalized || "<empty>"}`);
    }
    next[match[1]].add(match[2]);
  }
  return next;
}

function sameSet(left, right) {
  return left.size === right.size && [...left].every((value) => right.has(value));
}

function updateBook(venue, symbol, bidValue, askValue, sourceTimestamp, transport = "WS") {
  const normalizedSymbol = String(symbol || "").toUpperCase();
  if (!desired[venue]?.has(normalizedSymbol)) return;
  const bid = Number(bidValue);
  const ask = Number(askValue);
  if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0 || bid > ask) return;
  const receivedAt = Date.now();
  books.set(targetKey(venue, normalizedSymbol), {
    venue,
    symbol: normalizedSymbol,
    bid,
    ask,
    sourceUpdatedAt: safeTimestamp(sourceTimestamp, receivedAt),
    receivedAt,
    transport,
  });
}

async function jsonFetch(venue, symbol, url, options = {}) {
  if (!desired[venue]?.has(symbol)) throw new Error(`target removed before REST request: ${venue}:${symbol}`);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs || 3_000);
  marketRequests[venue] += 1;
  lastMarketRequestAt = Date.now();
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function fetchPulseUrl(url, timeoutMs = 3_000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const startedAt = Date.now();
  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        "Cache-Control": "no-cache",
        "User-Agent": "stock-review-mac/astro-cloud-pulse-relay",
      },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload || typeof payload !== "object" || payload.code !== 0) {
      throw new Error("invalid Pulse payload");
    }
    return { url, payload, durationMs: Date.now() - startedAt };
  } finally {
    clearTimeout(timer);
  }
}

async function pulseSnapshot(timeoutMs) {
  const fetchedAt = Date.now();
  const settled = await Promise.allSettled(
    PULSE_URLS.map((url) => fetchPulseUrl(url, timeoutMs)),
  );
  const successes = settled
    .filter((item) => item.status === "fulfilled")
    .map((item) => item.value);
  const failures = settled.flatMap((item, index) => (
    item.status === "rejected"
      ? [{ url: PULSE_URLS[index], error: String(item.reason?.message || item.reason) }]
      : []
  ));
  if (!successes.length) {
    throw new Error(`all Pulse sources failed: ${failures.map((item) => item.error).join("; ")}`);
  }
  const payloadJson = JSON.stringify(successes.map((item) => item.payload));
  const compressedPayload = zlib.gzipSync(Buffer.from(payloadJson, "utf8"), { level: 6 });
  return {
    status: failures.length ? "degraded" : "ok",
    fetchedAt: iso(fetchedAt),
    payloadEncoding: "gzip+base64",
    payloadsGzipBase64: compressedPayload.toString("base64"),
    payloadBytes: Buffer.byteLength(payloadJson),
    compressedBytes: compressedPayload.length,
    sourceCount: PULSE_URLS.length,
    successCount: successes.length,
    failureCount: failures.length,
    failures,
    sources: successes.map((item) => ({ url: item.url, durationMs: item.durationMs })),
  };
}

function needsRestRefresh(venue, symbol, maxAgeMs = 1_000) {
  if (!desired[venue]?.has(symbol)) return false;
  const book = books.get(targetKey(venue, symbol));
  return !book || Date.now() - book.receivedAt > maxAgeMs;
}

async function refreshBinanceRest(symbol) {
  if (!needsRestRefresh("bn", symbol)) return;
  const payload = await jsonFetch(
    "bn",
    symbol,
    `https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol=${encodeURIComponent(symbol)}`,
  );
  updateBook("bn", symbol, payload?.bidPrice, payload?.askPrice, payload?.time, "REST-confirmed");
}

async function refreshBitgetRest(symbol) {
  if (!needsRestRefresh("bg", symbol)) return;
  const payload = await jsonFetch(
    "bg",
    symbol,
    `https://api.bitget.com/api/v2/mix/market/ticker?symbol=${encodeURIComponent(symbol)}&productType=usdt-futures`,
  );
  const row = Array.isArray(payload?.data) ? payload.data[0] : null;
  updateBook("bg", symbol, row?.bidPr, row?.askPr, row?.ts, "REST-confirmed");
}

async function refreshGateRest(symbol) {
  if (!needsRestRefresh("gt", symbol)) return;
  const payload = await jsonFetch(
    "gt",
    symbol,
    `https://api.gateio.ws/api/v4/futures/usdt/order_book?contract=${encodeURIComponent(symbol)}&limit=1`,
  );
  const bid = payload?.bids?.[0]?.p ?? payload?.bids?.[0]?.[0];
  const ask = payload?.asks?.[0]?.p ?? payload?.asks?.[0]?.[0];
  const timestamp = Number(payload?.update || payload?.current || 0);
  updateBook("gt", symbol, bid, ask, timestamp > 0 && timestamp < 1e12 ? timestamp * 1000 : timestamp, "REST-confirmed");
}

async function refreshHyperliquidRest(symbol) {
  if (!needsRestRefresh("hl", symbol)) return;
  const payload = await jsonFetch("hl", symbol, "https://api.hyperliquid.xyz/info", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: "l2Book", coin: `xyz:${symbol}` }),
  });
  const bids = Array.isArray(payload?.levels?.[0]) ? payload.levels[0] : [];
  const asks = Array.isArray(payload?.levels?.[1]) ? payload.levels[1] : [];
  updateBook("hl", symbol, bids[0]?.px, asks[0]?.px, payload?.time, "REST-confirmed");
}

async function refreshQuietBooks() {
  if (stopping || restRefreshRunning) return;
  const jobs = [
    ...[...desired.bn].map(refreshBinanceRest),
    ...[...desired.bg].map(refreshBitgetRest),
    ...[...desired.gt].map(refreshGateRest),
    ...[...desired.hl].map(refreshHyperliquidRest),
  ];
  if (!jobs.length) return;
  restRefreshRunning = true;
  try {
    const results = await Promise.allSettled(jobs);
    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length && Date.now() - lastRestFailureLogAt >= 30_000) {
      lastRestFailureLogAt = Date.now();
      log(`REST confirmation partial failure ${failures.length}/${results.length}`);
    }
  } finally {
    restRefreshRunning = false;
  }
}

function stopVenue(venue) {
  const controller = controllers.get(venue);
  controllers.delete(venue);
  connections.delete(venue);
  if (!controller) return;
  controller.stopped = true;
  if (controller.reconnectTimer) clearTimeout(controller.reconnectTimer);
  if (controller.heartbeat) clearInterval(controller.heartbeat);
  if (controller.watchdog) clearInterval(controller.watchdog);
  const socket = controller.socket;
  controller.socket = null;
  if (socket) {
    sockets.delete(socket);
    try { socket.terminate(); } catch (_) { /* best effort */ }
  }
}

function connectVenue(venue, url, symbols, onOpen, onMessage) {
  const signature = symbols.join(",");
  const state = {
    venue,
    url,
    targets: [...symbols],
    status: "starting",
    reconnects: 0,
    messages: 0,
    openedAt: null,
    lastMessageAt: null,
    lastError: null,
  };
  const controller = {
    venue,
    signature,
    stopped: false,
    socket: null,
    heartbeat: null,
    watchdog: null,
    reconnectTimer: null,
    retryMs: 500,
  };
  connections.set(venue, state);
  controllers.set(venue, controller);

  const open = () => {
    if (stopping || controller.stopped || controllers.get(venue) !== controller) return;
    state.status = "connecting";
    const socket = new WebSocket(url, { handshakeTimeout: 8_000, perMessageDeflate: false });
    controller.socket = socket;
    sockets.add(socket);

    socket.on("open", () => {
      if (controller.stopped || controllers.get(venue) !== controller) {
        try { socket.terminate(); } catch (_) { /* best effort */ }
        return;
      }
      controller.retryMs = 500;
      state.status = "open";
      state.openedAt = Date.now();
      state.lastError = null;
      try {
        onOpen(socket, symbols);
      } catch (error) {
        state.lastError = `subscribe: ${error.message}`;
        socket.terminate();
        return;
      }
      controller.heartbeat = setInterval(() => {
        if (socket.readyState !== WebSocket.OPEN) return;
        try {
          socket.ping();
          if (venue === "bg") socket.send("ping");
        } catch (_) {
          socket.terminate();
        }
      }, 20_000);
      controller.watchdog = setInterval(() => {
        const reference = state.lastMessageAt || state.openedAt;
        if (reference && Date.now() - reference > 60_000) {
          state.lastError = "no messages for 60 seconds";
          socket.terminate();
        }
      }, 10_000);
    });

    socket.on("message", (raw) => {
      const text = String(raw);
      if (text === "pong") return;
      state.messages += 1;
      state.lastMessageAt = Date.now();
      try {
        onMessage(JSON.parse(text));
      } catch (error) {
        state.lastError = `message parse: ${error.message}`;
      }
    });

    socket.on("error", (error) => {
      state.lastError = error.message;
    });

    socket.on("close", (code, reason) => {
      sockets.delete(socket);
      if (controller.socket === socket) controller.socket = null;
      if (controller.heartbeat) clearInterval(controller.heartbeat);
      if (controller.watchdog) clearInterval(controller.watchdog);
      controller.heartbeat = null;
      controller.watchdog = null;
      if (stopping || controller.stopped || controllers.get(venue) !== controller) return;
      state.status = "closed";
      state.reconnects += 1;
      state.lastError = `closed ${code}${reason?.length ? `: ${String(reason)}` : ""}`;
      const delay = controller.retryMs;
      controller.retryMs = Math.min(15_000, controller.retryMs * 2);
      controller.reconnectTimer = setTimeout(open, delay);
    });
  };
  open();
}

function startVenue(venue) {
  const symbols = [...desired[venue]].sort();
  if (!symbols.length) return;
  if (venue === "bn") {
    connectVenue(
      venue,
      "wss://fstream.binance.com/public/stream",
      symbols,
      (socket, activeSymbols) => socket.send(JSON.stringify({
        method: "SUBSCRIBE",
        params: activeSymbols.map((symbol) => `${symbol.toLowerCase()}@bookTicker`),
        id: targetGeneration,
      })),
      (message) => {
        const row = message?.data || message;
        if (row?.e === "bookTicker") updateBook("bn", row.s, row.b, row.a, row.E || row.T);
      },
    );
    return;
  }
  if (venue === "bg") {
    connectVenue(
      venue,
      "wss://ws.bitget.com/v2/ws/public",
      symbols,
      (socket, activeSymbols) => socket.send(JSON.stringify({
        op: "subscribe",
        args: activeSymbols.map((instId) => ({ instType: "USDT-FUTURES", channel: "ticker", instId })),
      })),
      (message) => {
        if (message?.action !== "snapshot" && message?.action !== "update") return;
        for (const row of Array.isArray(message.data) ? message.data : []) {
          updateBook("bg", row.instId || message.arg?.instId, row.bidPr, row.askPr, row.ts);
        }
      },
    );
    return;
  }
  if (venue === "gt") {
    connectVenue(
      venue,
      "wss://fx-ws.gateio.ws/v4/ws/usdt",
      symbols,
      (socket, activeSymbols) => socket.send(JSON.stringify({
        time: Math.floor(Date.now() / 1000),
        channel: "futures.book_ticker",
        event: "subscribe",
        payload: activeSymbols,
      })),
      (message) => {
        if (message?.channel !== "futures.book_ticker" || message?.event !== "update") return;
        const row = message.result || {};
        updateBook("gt", row.s, row.b, row.a, row.t || message.time_ms);
      },
    );
    return;
  }
  connectVenue(
    venue,
    "wss://api.hyperliquid.xyz/ws",
    symbols,
    (socket, activeSymbols) => {
      for (const symbol of activeSymbols) {
        socket.send(JSON.stringify({
          method: "subscribe",
          subscription: { type: "l2Book", coin: `xyz:${symbol}` },
        }));
      }
    },
    (message) => {
      if (message?.channel !== "l2Book") return;
      const row = message.data || {};
      const symbol = String(row.coin || "").replace(/^xyz:/i, "").toUpperCase();
      const bids = Array.isArray(row.levels?.[0]) ? row.levels[0] : [];
      const asks = Array.isArray(row.levels?.[1]) ? row.levels[1] : [];
      updateBook("hl", symbol, bids[0]?.px, asks[0]?.px, row.time);
    },
  );
}

function applyTargets(rawTargets) {
  const next = parseTargets(rawTargets);
  const changedVenues = VENUES.filter((venue) => !sameSet(desired[venue], next[venue]));
  if (!changedVenues.length) return false;
  for (const venue of changedVenues) {
    for (const symbol of desired[venue]) {
      if (!next[venue].has(symbol)) books.delete(targetKey(venue, symbol));
    }
    desired[venue].clear();
    for (const symbol of next[venue]) desired[venue].add(symbol);
    stopVenue(venue);
  }
  targetGeneration += 1;
  targetUpdatedAt = Date.now();
  for (const venue of changedVenues) startVenue(venue);
  log(`targets generation=${targetGeneration} count=${targetList().length} [${targetList().join(",")}]`);
  if (targetList().length) void refreshQuietBooks();
  return true;
}

function snapshot(maxAgeMs) {
  const generatedAt = Date.now();
  const nested = { bn: {}, bg: {}, gt: {}, hl: {} };
  const stale = [];
  for (const book of books.values()) {
    if (!desired[book.venue]?.has(book.symbol)) continue;
    const ageMs = Math.max(0, generatedAt - book.receivedAt);
    const item = {
      bid: book.bid,
      ask: book.ask,
      sourceUpdatedAt: iso(book.sourceUpdatedAt),
      receivedAt: iso(book.receivedAt),
      ageMs,
      fresh: ageMs <= maxAgeMs,
      source: `Astro Cloud ${book.transport} · ${book.venue.toUpperCase()} ${book.symbol}`,
    };
    nested[book.venue][book.symbol] = item;
    if (!item.fresh) stale.push(targetKey(book.venue, book.symbol));
  }
  const activeTargets = targetList();
  const missing = activeTargets.filter((target) => {
    const [venue, symbol] = target.split(":", 2);
    return !nested[venue][symbol];
  });
  const requiredCount = activeTargets.length;
  const bookCount = Object.values(nested).reduce((total, venue) => total + Object.keys(venue).length, 0);
  return {
    status: requiredCount === 0 ? "idle" : missing.length || stale.length ? "warming" : "ok",
    generatedAt: iso(generatedAt),
    uptimeMs: generatedAt - startedAt,
    targets: activeTargets,
    targetGeneration,
    targetUpdatedAt: iso(targetUpdatedAt),
    books: nested,
    bookCount,
    requiredCount,
    missing,
    stale,
    activeConnectionCount: controllers.size,
    socketCount: sockets.size,
    marketRequestCount: Object.values(marketRequests).reduce((total, value) => total + value, 0),
    marketRequests: { ...marketRequests },
    lastMarketRequestAt: iso(lastMarketRequestAt),
    connections: Object.fromEntries([...connections.entries()].map(([venue, state]) => [venue, {
      status: state.status,
      targets: state.targets,
      reconnects: state.reconnects,
      messages: state.messages,
      openedAt: iso(state.openedAt),
      lastMessageAt: iso(state.lastMessageAt),
      lastError: state.lastError,
    }])),
  };
}

async function respond(request) {
  const id = request?.id || null;
  const command = request?.command || "snapshot";
  if (command === "shutdown") {
    applyTargets([]);
    process.stdout.write(`${JSON.stringify({ id, ok: true, result: snapshot(5_000) })}\n`);
    setTimeout(shutdown, 10).unref();
    return;
  }
  if (command === "pulse") {
    const timeoutMs = Math.max(1_000, Math.min(8_000, Number(request?.timeoutMs) || 3_000));
    try {
      const result = await pulseSnapshot(timeoutMs);
      process.stdout.write(`${JSON.stringify({ id, ok: true, result })}\n`);
    } catch (error) {
      process.stdout.write(`${JSON.stringify({ id, ok: false, error: error.message })}\n`);
    }
    return;
  }
  if (command === "dex_coins") {
    try {
      const { utils } = await loadDexRuntime();
      const coins = await utils.loadDexCoins();
      // Public asset identity only. Never load or return credentials/wallet data.
      const result = { coins: (Array.isArray(coins) ? coins : []).map(item => ({
        name: String(item?.name || ""), chainIndex: String(item?.chainIndex || ""),
        contractAddress: String(item?.contractAddress || "")
      })) };
      process.stdout.write(`${JSON.stringify({ id, ok: true, result })}\n`);
    } catch (_) {
      process.stdout.write(`${JSON.stringify({ id, ok: false, error: "Astro coin configuration unavailable" })}\n`);
    }
    return;
  }
  if (command === "dex_quote") {
    try {
      const result = await okxdexExecutableQuote(request);
      process.stdout.write(`${JSON.stringify({ id, ok: true, result })}\n`);
    } catch (error) {
      process.stdout.write(`${JSON.stringify({ id, ok: false, error: error.message })}\n`);
    }
    return;
  }
  if (command !== "snapshot" && command !== "health") {
    process.stdout.write(`${JSON.stringify({ id, ok: false, error: "unsupported command" })}\n`);
    return;
  }
  if (command === "snapshot") applyTargets(request?.targets ?? []);
  const maxAgeMs = Math.max(500, Math.min(60_000, Number(request?.maxAgeMs) || 5_000));
  process.stdout.write(`${JSON.stringify({ id, ok: true, result: snapshot(maxAgeMs) })}\n`);
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  try {
    void respond(JSON.parse(line)).catch((error) => {
      process.stdout.write(`${JSON.stringify({ id: null, ok: false, error: error.message })}\n`);
    });
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ id: null, ok: false, error: error.message })}\n`);
  }
});

function shutdown() {
  if (stopping) return;
  stopping = true;
  for (const venue of VENUES) stopVenue(venue);
  for (const socket of sockets) {
    try { socket.terminate(); } catch (_) { /* best effort */ }
  }
  setTimeout(() => process.exit(0), 50).unref();
}

input.on("close", shutdown);
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
setInterval(refreshQuietBooks, 1_000);
log("started idle read-only relay; waiting for exact visible-card targets");
