const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

export const RANGE_DURATIONS_MS = Object.freeze({
  "24h": DAY_MS,
  "7d": 7 * DAY_MS,
  "30d": 30 * DAY_MS,
});

const RANGE_LABELS = Object.freeze({
  "24h": "24 hours",
  "7d": "7 days",
  "30d": "30 days",
});

export const RANGE_PRESETS = Object.freeze([
  { value: "24h", label: RANGE_LABELS["24h"], durationMs: RANGE_DURATIONS_MS["24h"] },
  { value: "7d", label: RANGE_LABELS["7d"], durationMs: RANGE_DURATIONS_MS["7d"] },
  { value: "30d", label: RANGE_LABELS["30d"], durationMs: RANGE_DURATIONS_MS["30d"] },
]);

function durationMs(preset) {
  const duration = RANGE_DURATIONS_MS[preset];
  if (duration == null) throw new RangeError(`Unknown range preset: ${preset}`);
  return duration;
}

function milliseconds(value) {
  const date = value instanceof Date ? new Date(value.getTime()) : new Date(value);
  const result = date.getTime();
  if (!Number.isFinite(result)) throw new RangeError("Range timestamps must be valid dates");
  return result;
}

function createRange(preset, endMilliseconds) {
  const duration = durationMs(preset);
  const end = new Date(endMilliseconds);
  const start = new Date(endMilliseconds - duration);
  return { preset, start: start.toISOString(), end: end.toISOString() };
}

export function rangeLabel(preset) {
  durationMs(preset);
  return RANGE_LABELS[preset];
}

export function latestRange(preset, now = new Date()) {
  return createRange(preset, milliseconds(now));
}

export function selectPreset(preset, now = new Date()) {
  return latestRange(preset, now);
}

export function shiftRange(range, direction, now = new Date()) {
  const duration = durationMs(range.preset);
  const currentEnd = milliseconds(range.end);
  const currentTime = milliseconds(now);
  let nextEnd;
  if (direction === "previous" || direction === -1) {
    nextEnd = currentEnd - duration;
  } else if (direction === "next" || direction === 1) {
    nextEnd = Math.min(currentEnd + duration, currentTime);
  } else {
    throw new RangeError(`Unknown range direction: ${direction}`);
  }
  return createRange(range.preset, nextEnd);
}
