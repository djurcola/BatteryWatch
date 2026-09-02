import assert from "node:assert/strict";
import { test } from "node:test";

import { calculateVisibleNetValue } from "../src/visible-net-value.mjs";

const points = [
  { timestamp: "2026-01-01T00:00:00Z", net_energy_value_aud: 1 },
  { timestamp: "2026-01-01T00:05:00Z", net_energy_value_aud: 2 },
  // The source data can have missing five-minute intervals.
  { timestamp: "2026-01-01T00:25:00Z", net_energy_value_aud: 10 },
  { timestamp: "2026-01-01T00:30:00Z", net_energy_value_aud: 20 },
  { timestamp: "2026-01-01T00:35:00Z", net_energy_value_aud: 30 },
  { timestamp: "2026-01-01T01:00:00Z", net_energy_value_aud: 4 },
];

test("percentage zoom ranges are mapped to elapsed time, not point indexes", () => {
  // 00:22:30–00:37:30 is 37.5%–62.5% of the one-hour time axis.
  assert.equal(calculateVisibleNetValue(points, { start: 37.5, end: 62.5 }), 60);
});

test("explicit time values take precedence over percentage values", () => {
  assert.equal(
    calculateVisibleNetValue(points, {
      start: 0,
      end: 100,
      startValue: "2026-01-01T00:25:00Z",
      endValue: "2026-01-01T00:35:00Z",
    }),
    60,
  );
});

test("null interval values contribute zero to the visible total", () => {
  assert.equal(
    calculateVisibleNetValue(
      [
        { timestamp: "2026-01-01T00:00:00Z", net_energy_value_aud: null },
        { timestamp: "2026-01-01T00:05:00Z", net_energy_value_aud: 7.5 },
      ],
      { start: 0, end: 100 },
    ),
    7.5,
  );
});
