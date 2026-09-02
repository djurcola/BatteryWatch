export type RangePreset = "24h" | "7d" | "30d" | "custom";
export type RangeDirection = "previous" | "next";
export type HistoryRange = {
  preset: RangePreset;
  start: string;
  end: string;
};
export type RangeNow = Date | string | number;

export const RANGE_DURATIONS_MS: Readonly<Record<RangePreset, number | null>>;
export const RANGE_PRESETS: readonly {
  value: RangePreset;
  label: string;
  durationMs: number | null;
}[];
export function rangeLabel(preset: RangePreset): string;
export function customRange(start: RangeNow, end: RangeNow): HistoryRange;
export function latestRange(preset: RangePreset, now?: RangeNow): HistoryRange;
export function selectPreset(preset: RangePreset, now?: RangeNow): HistoryRange;
export function shiftRange(
  range: HistoryRange,
  direction: RangeDirection | -1 | 1,
  now?: RangeNow,
): HistoryRange;
