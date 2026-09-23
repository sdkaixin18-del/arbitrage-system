import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { readLiveAnnouncements } from './read-live-announcements.mjs';
const require = createRequire(import.meta.url), ts = require('typescript');
const exports = {};
runInNewContext(ts.transpileModule(readFileSync('src/index.ts','utf8')+'\nexport {listingRowsFromText};', {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,
  {exports,require:name=>name==='cloudflare:workers'?{DurableObject:class{}}:{},URL,Date,console,Headers,Response,Request,crypto});
const groups = new Map();
for(const row of readLiveAnnouncements()) {
  if(row.detailStatus!=='ambiguous'||!row.detailText)continue;
  groups.set(row.exchange+':'+row.url, [...(groups.get(row.exchange+':'+row.url)||[]),row]);
}
const counts={},remaining=[];
for(const rows of groups.values()) {
  const base={...rows[0],symbol:null,symbols:[...new Set(rows.flatMap(r=>r.symbols||[r.symbol]).filter(Boolean))],scheduledAt:null};
  const parsed=exports.listingRowsFromText(base,base.detailText);
  const c=counts[base.exchange]??={total:0,resolved:0};c.total++;
  if(parsed.every(r=>r.detailStatus==='parsed'))c.resolved++;
  else remaining.push({exchange:base.exchange,title:base.title,rows:parsed.filter(r=>r.detailStatus!=='parsed').map(r=>r.symbol),lines:base.detailText.split(/\n+/).filter(l=>/2026|上线|上線|list|trad/i.test(l)).slice(-6)});
}
console.log(JSON.stringify({counts,remaining:remaining.filter(r=>r.exchange!=='aster').slice(0,15)},null,2));
