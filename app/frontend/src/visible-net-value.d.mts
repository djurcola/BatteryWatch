export type VisibleNetValuePoint = {
  timestamp: string;
  net_energy_value_aud: number | null;
};

export type DataZoomRange = {
  start?: number;
  end?: number;
  startValue?: unknown;
  endValue?: unknown;
  batch?: DataZoomRange[];
};

export function calculateVisibleNetValue(
  points: VisibleNetValuePoint[],
  event: DataZoomRange,
): number;
