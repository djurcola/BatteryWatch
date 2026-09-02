import type { HistoryRange, RangeNow, RangePreset } from "./history-range.mjs";

export type HistoryBufferBounds = {
  start: number;
  end: number;
};

export type HistoryBufferBoundsInput = {
  start: RangeNow;
  end: RangeNow;
};

export type HistoryChunk = {
  direction: "previous" | "next";
  start: string;
  end: string;
};

export type HistoryBufferPoint = {
  timestamp: string;
};

export const MAX_API_WINDOW_MS: number;
export function chunkDurationMs(preset: Exclude<RangePreset, "custom">): number;
export function initialGutterChunks(range: HistoryRange, now?: RangeNow): HistoryChunk[];
export function initialGutterChunk(range: HistoryRange, now?: RangeNow): HistoryChunk | null;
export function planAdjacentChunk(options: {
  range: HistoryRange;
  loadedStart: RangeNow;
  loadedEnd: RangeNow;
  visibleStart: RangeNow;
  visibleEnd: RangeNow;
  now?: RangeNow;
  chunkMs?: number;
}): HistoryChunk | null;
export function mergePoints<T extends HistoryBufferPoint>(existing: T[], incoming: T[]): T[];
export function resolveVisibleBounds(
  event: unknown,
  extent: HistoryBufferBoundsInput,
  now?: RangeNow,
): HistoryBufferBounds;
export function retainPoints<T extends HistoryBufferPoint>(
  points: T[],
  visible: HistoryBufferBoundsInput,
  options?: { beforeMs?: number; afterMs?: number },
): T[];
export function retainBuffer<T extends HistoryBufferPoint>(
  points: T[],
  visible: HistoryBufferBoundsInput,
  requestedCoverage: HistoryBufferBoundsInput,
  options?: { beforeMs?: number; afterMs?: number },
): { points: T[]; loadedStart: number; loadedEnd: number };
