type GapPoint = {openSpreadPct: number | null; closeSpreadPct: number | null};

// Same-time curve distance, in percentage points. Never pair different timestamps.
export function bookGap(point: GapPoint | undefined): number | null {
  if (!point || typeof point.openSpreadPct !== "number" || typeof point.closeSpreadPct !== "number"
    || !Number.isFinite(point.openSpreadPct) || !Number.isFinite(point.closeSpreadPct)) return null;
  const gap = point.closeSpreadPct - point.openSpreadPct;
  // Crossed or inconsistent quotes are not made plausible with an absolute value.
  return Number.isFinite(gap) && gap >= -1e-9 ? Math.max(0, gap) : null;
}

export function bookGapStats(points: GapPoint[]) {
  const values = points.map(bookGap).filter((v): v is number => v !== null);
  const average = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  const latest = bookGap(points[points.length - 1]);
  const difference = latest !== null && average !== null ? latest - average : null;
  const maximum = values.length ? Math.max(...values) : null;
  const minimum = values.length ? Math.min(...values) : null;
  return {average, latest, difference, maximum, minimum, samples: values.length};
}
