import { DatabaseSync } from 'node:sqlite';
import { deserialize } from 'node:v8';

// Read-only production evidence. Never open a second Miniflare on live storage.
export function readLiveAnnouncements() {
  const db = new DatabaseSync('/home/example/Documents/套利系统/runtime/exchange-news/storage/do/exchange-news-local-ExchangeMonitor/34f7bf0bcf5187bada82029728fd0184db430ef365d053e1d3a567dbaeec4c63.sqlite', { readOnly: true });
  try {
    const kv = new Map(db.prepare("select key,value from _cf_KV where key like 'state:v3:%'").all().map(r => [r.key, deserialize(r.value)]));
    const join = (key, count) => Array.from({ length: count }, (_, i) => kv.get(key + i)).join('');
    const manifest = JSON.parse(join('state:v3:manifest:', +kv.get('state:v3:parts')));
    return manifest.announcements.records.map(([, key, count]) => JSON.parse(join(key, count)));
  } finally { db.close(); }
}
