// Record-level checkpoints: changing a book counter must not rewrite all
// inventory, announcement bodies and historical logs. V2 remains read-only
// during migration so a failed V3 transaction cannot destroy the old state.
const PREFIX = 'state:v3:';
const CHUNK_SIZE = 16_000;
type RecordRef = [string, string, number];
type Manifest = Record<string, { shape: 'array' | 'object' | 'value'; records: RecordRef[] }>;
export type StateWriteCache = Map<string, string>;

async function getMany(storage: DurableObjectStorage, keys: string[]) {
  const result = new Map<string, string>();
  for (let offset = 0; offset < keys.length; offset += 100) {
    for (const [key, value] of await storage.get<string>(keys.slice(offset, offset + 100))) result.set(key, value);
  }
  return result;
}

function joinParts(values: Map<string, string>, base: string, count: number) {
  let result = '';
  for (let i = 0; i < count; i++) {
    const part = values.get(base + i);
    if (typeof part !== 'string') throw new Error('持久化状态分片缺失，禁止重建空基线');
    result += part;
  }
  return result;
}

export async function readState<T>(storage: DurableObjectStorage, cache: StateWriteCache = new Map()): Promise<T | null> {
  const countValue = await storage.get<string>(PREFIX + 'parts');
  if (countValue !== undefined) {
    const count = Number(countValue);
    if (!Number.isInteger(count) || count < 1 || count > 10_000) throw new Error('持久化状态索引异常，禁止重建空基线');
    const manifestParts = await getMany(storage, Array.from({ length: count }, (_, i) => PREFIX + 'manifest:' + i));
    const manifest = JSON.parse(joinParts(manifestParts, PREFIX + 'manifest:', count)) as Manifest;
    const keys = Object.values(manifest).flatMap(section => section.records.flatMap(([, base, parts]) =>
      Array.from({ length: parts }, (_, i) => base + i)));
    const records = await getMany(storage, keys);
    const restored: Record<string, unknown> = {};
    for (const [field, section] of Object.entries(manifest)) {
      const values = section.records.map(([name, base, parts]) => [name, JSON.parse(joinParts(records, base, parts))] as const);
      restored[field] = section.shape === 'array' ? values.map(([, value]) => value)
        : section.shape === 'object' ? Object.fromEntries(values) : values[0]?.[1];
    }
    cache.clear();
    for (const [key, value] of [...manifestParts, ...records]) cache.set(key, value);
    cache.set(PREFIX + 'parts', countValue);
    return restored as T;
  }
  const oldCount = await storage.get<number>('state:parts');
  if (!oldCount) return null;
  const oldParts = await getMany(storage, Array.from({ length: oldCount }, (_, i) => 'state:part:' + i));
  return JSON.parse(joinParts(oldParts, 'state:part:', oldCount)) as T;
}

export async function writeState(storage: DurableObjectStorage, state: object, cache: StateWriteCache = new Map()): Promise<number> {
  const entries = new Map<string, string>();
  const manifest: Manifest = {};
  const add = (base: string, value: unknown) => {
    const serialized = JSON.stringify(value);
    let count = 0;
    for (let offset = 0; offset < serialized.length; offset += CHUNK_SIZE) entries.set(base + count++, serialized.slice(offset, offset + CHUNK_SIZE));
    return count;
  };
  // Detach before awaiting digests; news and book completions may mutate the
  // live monitor object while this checkpoint is being encoded.
  const snapshot = JSON.parse(JSON.stringify(state)) as Record<string, unknown>;
  for (const [field, value] of Object.entries(snapshot)) {
    if (value === undefined) continue;
    const shape = Array.isArray(value) ? 'array' : field === 'inventory' ? 'object' : 'value';
    const records: Array<[string, unknown]> = Array.isArray(value) ? value.map((row: Record<string, unknown>, index: number) => [
      String(row?.key ?? row?.id ?? (field === 'newsHourlyStats' ? row.exchange + ':' + row.hour : index)), row,
    ]) : shape === 'object' && value && typeof value === 'object' ? Object.entries(value) : [['value', value]];
    const used = new Set<string>();
    manifest[field] = { shape, records: await Promise.all(records.map(async ([name, row], index): Promise<RecordRef> => {
      let identity = String(name);
      if (used.has(identity)) identity += ':duplicate:' + index;
      used.add(identity);
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(field + ':' + identity));
      const hash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
      const base = PREFIX + 'record:' + hash + ':';
      return [String(name), base, add(base, row)];
    })) };
  }
  const count = add(PREFIX + 'manifest:', manifest);
  entries.set(PREFIX + 'parts', String(count));
  const changed = [...entries].filter(([key, value]) => cache.get(key) !== value);
  const retired = [...cache.keys()].filter(key => !entries.has(key));
  if (!changed.length && !retired.length) return 0;
  await storage.transaction(async transaction => {
    for (let offset = 0; offset < changed.length; offset += 100) await transaction.put(Object.fromEntries(changed.slice(offset, offset + 100)));
    for (let offset = 0; offset < retired.length; offset += 100) await transaction.delete(retired.slice(offset, offset + 100));
  });
  // Commit cache only after durable success. A failed write remains retryable.
  cache.clear();
  for (const [key, value] of entries) cache.set(key, value);
  return changed.length + retired.length;
}
