const NOTE_MARKER = "OKXDEX链：";
const NOTE_LIMIT = 100;
const POLL_INTERVAL_MS = 5000;
const REQUEST_TIMEOUT_MS = 1200;
const CHAIN_LABELS = {
  "1": "Ethereum",
  "56": "BNB Smart Chain",
  "501": "Solana",
  "8453": "Base",
  "42161": "Arbitrum One",
};

function normalizeSymbol(value) {
  return String(value || "").trim().toUpperCase().replace(/USDT$/, "");
}

function collectObjects(value, predicate, result = [], seen = new Set()) {
  if (!value || typeof value !== "object" || seen.has(value)) return result;
  seen.add(value);
  if (!Array.isArray(value) && predicate(value)) result.push(value);
  const children = Array.isArray(value) ? value : Object.values(value);
  for (const child of children) collectObjects(child, predicate, result, seen);
  return result;
}

function chainLabel(chainIndex) {
  const normalized = String(chainIndex || "").trim();
  return CHAIN_LABELS[normalized] || normalized || "未知链";
}

function mergeChainNote(currentText, label) {
  const markerText = `${NOTE_MARKER}${label}`;
  const current = String(currentText || "").trim();
  if (!current) return markerText;
  const markerPattern = /(?:^|\s*·\s*)OKXDEX链：[^·\n]+/u;
  if (markerPattern.test(current)) {
    return current.replace(markerPattern, (match) => {
      const separator = match.startsWith(" · ") ? " · " : "";
      return `${separator}${markerText}`;
    }).trim();
  }
  return `${current} · ${markerText}`;
}

function parseStoredNotes(raw) {
  try {
    const value = JSON.parse(raw || "[]");
    return Array.isArray(value) ? value.filter((item) => item && typeof item.id === "string") : [];
  } catch {
    return [];
  }
}

async function fetchJson(path, init = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      credentials: "same-origin",
      ...init,
      signal: controller.signal,
    });
    if (!response.ok) return null;
    const payload = await response.json();
    return payload && typeof payload === "object" ? payload : null;
  } catch {
    return null;
  } finally {
    window.clearTimeout(timeout);
  }
}

export function applyOkxdexChainNotes(configPayload, dexPayload, storage = window.localStorage) {
  const pairs = collectObjects(
    configPayload,
    (item) => typeof item.id === "string"
      && String(item.type || "").toUpperCase() === "SF"
      && String(item.buyEx || "").toLowerCase() === "okxdex"
      && Boolean(normalizeSymbol(item.name)),
  );
  const coins = collectObjects(
    dexPayload,
    (item) => Boolean(normalizeSymbol(item.name)) && String(item.chainIndex || "").trim() !== "",
  );
  if (!pairs.length || !coins.length) return false;

  const chainBySymbol = new Map();
  for (const coin of coins) {
    const symbol = normalizeSymbol(coin.name);
    if (!chainBySymbol.has(symbol)) chainBySymbol.set(symbol, chainLabel(coin.chainIndex));
  }

  const storageKey = "coin_pairs_notes";
  const previousRaw = storage.getItem(storageKey) || "[]";
  const notes = parseStoredNotes(previousRaw);
  const noteById = new Map(notes.map((item) => [item.id, item]));
  let changed = false;
  for (const pair of pairs) {
    const label = chainBySymbol.get(normalizeSymbol(pair.name));
    if (!label) continue;
    const previous = noteById.get(pair.id);
    const text = mergeChainNote(previous?.text, label);
    if (previous?.text === text) continue;
    noteById.set(pair.id, { id: pair.id, text, updatedAt: Date.now() });
    changed = true;
  }
  if (!changed) return false;

  const next = [...noteById.values()]
    .sort((left, right) => Number(right.updatedAt || 0) - Number(left.updatedAt || 0))
    .slice(0, NOTE_LIMIT);
  const nextRaw = JSON.stringify(next);
  storage.setItem(storageKey, nextRaw);
  try {
    window.dispatchEvent(new StorageEvent("storage", { key: storageKey, oldValue: previousRaw, newValue: nextRaw }));
  } catch {
    // StorageEvent is only an optimization; a one-time reload handles older browsers.
  }
  return true;
}

export async function syncOkxdexChainNotes() {
  const [configPayload, dexPayload] = await Promise.all([
    fetchJson("./api/config"),
    fetchJson("./api/config/dex-coins", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "list" }),
    }),
  ]);
  if (!configPayload || !dexPayload) return false;
  return applyOkxdexChainNotes(configPayload, dexPayload);
}

export function startOkxdexChainNoteSync() {
  let running = false;
  window.setInterval(async () => {
    if (running) return;
    running = true;
    try {
      const changed = await syncOkxdexChainNotes();
      if (changed && window.location.pathname.includes("/dashboard")) window.location.reload();
    } finally {
      running = false;
    }
  }, POLL_INTERVAL_MS);
}

