const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
// Avoid issuing a sub-interval request while a latest snapshot ages between renders.
const MIN_FORWARD_GUTTER_MS = 5 * 60 * 1000;

const PRESET_DURATIONS_MS = Object.freeze({
  "24h": DAY_MS,
  "7d": 7 * DAY_MS,
  "30d": 30 * DAY_MS,
});

function milliseconds(value) {
  const result = value instanceof Date ? value.getTime() : typeof value === "number" ? value : Date.parse(String(value));
  if (!Number.isFinite(result)) throw new RangeError("History buffer timestamps must be valid dates");
  return result;
}

function iso(millisecondsValue) {
  return new Date(millisecondsValue).toISOString();
}

function pointMilliseconds(point) {
  try {
    return milliseconds(point.timestamp);
  } catch {
    return null;
  }
}

export function mergePoints(existing, incoming) {
  const byTimestamp = new Map();
  for (const point of [...existing, ...incoming]) {
    const pointTime = pointMilliseconds(point);
    const key = pointTime == null ? `raw:${String(point.timestamp)}` : `time:${pointTime}`;
    byTimestamp.set(key, point);
  }
  return [...byTimestamp.values()].sort((left, right) => {
    const leftTime = pointMilliseconds(left);
    const rightTime = pointMilliseconds(right);
    if (leftTime == null || rightTime == null) return 0;
    return leftTime - rightTime;
  });
}

function zoomRange(event) {
  if (!event || typeof event !== "object") return {};
  const candidates = Array.isArray(event.batch) ? event.batch : [];
  return candidates.find((candidate) => candidate && (candidate.startValue != null || candidate.endValue != null))
    ?? candidates[0]
    ?? event;
}

function percent(value, fallback) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.min(100, value))
    : fallback;
}

export function resolveVisibleBounds(event, extent, now) {
  const extentStart = milliseconds(extent.start);
  const extentEnd = milliseconds(extent.end);
  const zoom = zoomRange(event);
  const startValue = pointMilliseconds({ timestamp: zoom.startValue });
  const endValue = pointMilliseconds({ timestamp: zoom.endValue });
  let start = startValue ?? extentStart + (extentEnd - extentStart) * percent(zoom.start, 0) / 100;
  let end = endValue ?? extentStart + (extentEnd - extentStart) * percent(zoom.end, 100) / 100;
  if (now != null) {
    const nowMilliseconds = milliseconds(now);
    start = Math.min(start, nowMilliseconds);
    end = Math.min(end, nowMilliseconds);
  }
  return { start: Math.min(start, end), end: Math.max(start, end) };
}

export function retainPoints(points, visible, { beforeMs = 0, afterMs = 0 } = {}) {
  const visibleStart = milliseconds(visible.start);
  const visibleEnd = milliseconds(visible.end);
  const before = Math.max(0, beforeMs);
  const after = Math.max(0, afterMs);
  const lowerBound = visibleStart - before;
  const upperBound = visibleEnd + after;
  return points.filter((point) => {
    const pointTime = pointMilliseconds(point);
    return pointTime != null && pointTime >= lowerBound && pointTime <= upperBound;
  });
}

export function retainBuffer(points, visible, requestedCoverage, options = {}) {
  const visibleStart = milliseconds(visible.start);
  const visibleEnd = milliseconds(visible.end);
  const requestedStart = milliseconds(requestedCoverage.start);
  const requestedEnd = milliseconds(requestedCoverage.end);
  if (requestedEnd < requestedStart) throw new RangeError("History buffer coverage must be ordered");
  const before = Math.max(0, options.beforeMs ?? 0);
  const after = Math.max(0, options.afterMs ?? 0);
  return {
    points: retainPoints(points, visible, { beforeMs: before, afterMs: after }),
    loadedStart: Math.max(requestedStart, visibleStart - before),
    loadedEnd: Math.min(requestedEnd, visibleEnd + after),
  };
}

export const MAX_API_WINDOW_MS = 30 * DAY_MS;

export function chunkDurationMs(preset) {
  const duration = PRESET_DURATIONS_MS[preset];
  if (duration == null) throw new RangeError("Custom ranges do not page automatically");
  return Math.min(MAX_API_WINDOW_MS, duration / 6);
}

export function initialGutterChunks(range, now = new Date()) {
  if (range.preset === "custom") return [];
  const nowMs = milliseconds(now);
  const size = chunkDurationMs(range.preset);
  const previousEndMs = Math.min(milliseconds(range.start), nowMs);
  const chunks = [{
    direction: "previous",
    start: iso(previousEndMs - size),
    end: iso(previousEndMs),
  }];
  const selectedEndMs = milliseconds(range.end);
  const nextStartMs = Math.min(selectedEndMs, nowMs);
  const nextEndMs = Math.min(nowMs, selectedEndMs + size);
  if (nextEndMs - nextStartMs >= MIN_FORWARD_GUTTER_MS) {
    chunks.push({
      direction: "next",
      start: iso(nextStartMs),
      end: iso(nextEndMs),
    });
  }
  return chunks;
}

export function initialGutterChunk(range, now = new Date()) {
  return initialGutterChunks(range, now)[0] ?? null;
}

function chunkDuration(range, requestedChunkMs) {
  if (requestedChunkMs != null) return Math.min(MAX_API_WINDOW_MS, requestedChunkMs);
  return chunkDurationMs(range.preset);
}

export function planAdjacentChunk({ range, loadedStart, loadedEnd, visibleStart, visibleEnd, now = new Date(), chunkMs }) {
  if (range.preset === "custom") return null;
  const nowMs = milliseconds(now);
  const loadedStartMs = Math.min(milliseconds(loadedStart), nowMs);
  const loadedEndMs = Math.min(milliseconds(loadedEnd), nowMs);
  const visibleStartMs = milliseconds(visibleStart);
  const visibleEndMs = milliseconds(visibleEnd);
  const size = chunkDuration(range, chunkMs);
  if (!(size > 0)) throw new RangeError("History buffer chunks must be positive");
  const threshold = size / 2;

  if (visibleStartMs <= loadedStartMs + threshold) {
    const endMs = loadedStartMs;
    const startMs = Math.min(endMs, endMs - size);
    return {
      direction: "previous",
      start: iso(startMs),
      end: iso(endMs),
    };
  }
  if (visibleEndMs >= loadedEndMs - threshold && loadedEndMs < nowMs) {
    const startMs = loadedEndMs;
    const endMs = Math.min(nowMs, startMs + size);
    if (endMs - startMs >= MIN_FORWARD_GUTTER_MS) {
      return {
        direction: "next",
        start: iso(startMs),
        end: iso(endMs),
      };
    }
  }
  return null;
}
