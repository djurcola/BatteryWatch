function timestamp(value) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (value instanceof Date) {
    const milliseconds = value.getTime();
    return Number.isFinite(milliseconds) ? milliseconds : null;
  }
  if (typeof value === "string") {
    const milliseconds = Date.parse(value);
    return Number.isFinite(milliseconds) ? milliseconds : null;
  }
  return null;
}

function percent(value, fallback) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.min(100, value))
    : fallback;
}

function zoomRange(event) {
  if (!event || typeof event !== "object") return {};
  const candidates = Array.isArray(event.batch) ? event.batch : [];
  return candidates.find((candidate) => candidate && (candidate.startValue != null || candidate.endValue != null))
    ?? candidates[0]
    ?? event;
}

export function calculateVisibleNetValue(points, event) {
  if (points.length === 0) return 0;

  const zoom = zoomRange(event);
  const pointTimes = points.map((point) => timestamp(point.timestamp)).filter((value) => value != null);
  if (pointTimes.length === 0) return 0;

  const firstTime = Math.min(...pointTimes);
  const lastTime = Math.max(...pointTimes);
  const startPercent = percent(zoom.start, 0);
  const endPercent = Math.max(startPercent, percent(zoom.end, 100));
  const startTime = timestamp(zoom.startValue) ?? firstTime + (lastTime - firstTime) * startPercent / 100;
  const endTime = timestamp(zoom.endValue) ?? firstTime + (lastTime - firstTime) * endPercent / 100;
  const rangeStart = Math.min(startTime, endTime);
  const rangeEnd = Math.max(startTime, endTime);

  return points.reduce((total, point) => {
    const pointTime = timestamp(point.timestamp);
    if (pointTime == null || pointTime < rangeStart || pointTime > rangeEnd) return total;
    return total + (typeof point.net_energy_value_aud === "number" ? point.net_energy_value_aud : 0);
  }, 0);
}
