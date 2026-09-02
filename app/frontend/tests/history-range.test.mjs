import assert from "node:assert/strict";
import { test } from "node:test";

import { customRange, latestRange, selectPreset, shiftRange } from "../src/history-range.mjs";

const now = "2026-09-02T12:00:00Z";

test("latest ranges use the selected duration and an injected current time", () => {
  assert.deepEqual(latestRange("24h", now), {
    preset: "24h",
    start: "2026-09-01T12:00:00.000Z",
    end: "2026-09-02T12:00:00.000Z",
  });
  assert.deepEqual(latestRange("7d", now), {
    preset: "7d",
    start: "2026-08-26T12:00:00.000Z",
    end: "2026-09-02T12:00:00.000Z",
  });
  assert.deepEqual(latestRange("30d", now), {
    preset: "30d",
    start: "2026-08-03T12:00:00.000Z",
    end: "2026-09-02T12:00:00.000Z",
  });
});

test("previous and next shift by one selected duration", () => {
  const current = latestRange("7d", now);
  assert.deepEqual(shiftRange(current, "previous", now), {
    preset: "7d",
    start: "2026-08-19T12:00:00.000Z",
    end: "2026-08-26T12:00:00.000Z",
  });
  assert.deepEqual(
    shiftRange(shiftRange(current, "previous", now), "next", now),
    current,
  );
});

test("next caps its end at now and preset selection returns the latest window", () => {
  const current = latestRange("24h", now);
  assert.deepEqual(shiftRange(current, "next", now), current);
  assert.deepEqual(selectPreset("30d", now), latestRange("30d", now));
});

test("custom ranges require explicit ordered bounds and fixed helpers reject custom", () => {
  assert.deepEqual(
    customRange("2026-08-03T12:00:00Z", "2026-09-02T12:00:00Z"),
    {
      preset: "custom",
      start: "2026-08-03T12:00:00.000Z",
      end: "2026-09-02T12:00:00.000Z",
    },
  );
  assert.throws(
    () => customRange("2026-09-02T12:00:00Z", "2026-08-03T12:00:00Z"),
    /precede/,
  );
  assert.throws(() => latestRange("custom", now), /explicit bounds/);
});
