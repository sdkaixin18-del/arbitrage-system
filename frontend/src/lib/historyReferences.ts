export type ReferencePoint = {timestamp: number; spreadPct: number};

// Descriptive, in-sample reference levels. These are not executable prices or a backtest.
export function historyReferences(points: ReferencePoint[], intervalMs: number) {
  const rows = [...new Map(points.filter(p => Number.isFinite(p.timestamp) && Number.isFinite(p.spreadPct))
    .map(p => [p.timestamp, p])).values()].sort((a,b) => a.timestamp-b.timestamp);
  const base = {samples: rows.length, open: null as number | null, close: null as number | null,
    completed: 0, unresolved: 0, interrupted: 0, medianMinutes: null as number | null, reason: ""};
  if (rows.length < 30) return {...base, reason: `有效历史点 ${rows.length}/30，暂不生成历史参考`};
  const values = rows.map(p=>p.spreadPct).sort((a,b)=>a-b);
  const quantile = (sorted: number[], q: number) => {
    const index=(sorted.length-1)*q, low=Math.floor(index), weight=index-low;
    return sorted[low]+(sorted[Math.min(low+1,sorted.length-1)]-sorted[low])*weight;
  };
  const open=quantile(values,.9), close=quantile(values,.5);
  if (open <= 0) return {...base, reason: "历史高位差价未高于零，暂不生成买 DEX／空合约参考"};
  if (open-close < 1e-8) return {...base, reason: "历史高位与中位数接近，无法区分开平仓参考"};
  const durations: number[]=[];
  let started: number | null=null, interrupted=0;
  rows.forEach((point,index)=>{
    // Do not count an apparent convergence across missing candles.
    if(index>0 && point.timestamp-rows[index-1].timestamp>intervalMs*1.5){
      if(started!==null) interrupted++;
      started=null;
    }
    if(started===null && point.spreadPct>=open) started=point.timestamp;
    else if(started!==null && point.spreadPct<=close){
      durations.push((point.timestamp-started)/60000);started=null;
    }
  });
  return {...base, open, close, completed:durations.length, unresolved:started===null?0:1, interrupted,
    medianMinutes:durations.length?quantile(durations.sort((a,b)=>a-b),.5):null};
}
