import assert from "node:assert/strict";
import { test } from "node:test";

import * as historyBuffer from "../src/history-buffer.mjs";

const now = "2026-09-02T12:00:00Z";
const range = {
  preset: "24h",
  start: "2026-09-01T12:00:00.000Z",
  end: now,
};

test("plans an older bounded chunk when the visible window reaches the loaded start", () => {
  assert.deepEqual(
    historyBuffer.planAdjacentChunk({
      range,
      loadedStart: "2026-09-01T06:00:00.000Z",
      loadedEnd: range.end,
      visibleStart: "2026-09-01T06:00:00.000Z",
      visibleEnd: "2026-09-02T06:00:00.000Z",
      now,
    }),
    {
      direction: "previous",
      start: "2026-09-01T02:00:00.000Z",
      end: "2026-09-01T06:00:00.000Z",
    },
  );
});

test("caps a forward adjacent chunk at the supplied current time", () => {
  const sevenDayRange = {
    preset: "7d",
    start: "2026-08-26T12:00:00.000Z",
    end: now,
  };
  assert.deepEqual(
    historyBuffer.planAdjacentChunk({
      range: sevenDayRange,
      loadedStart: "2026-08-25T12:00:00.000Z",
      loadedEnd: "2026-09-02T10:00:00.000Z",
      visibleStart: "2026-08-26T12:00:00.000Z",
      visibleEnd: "2026-09-02T10:00:00.000Z",
      now,
    }),
    {
      direction: "next",
      start: "2026-09-02T10:00:00.000Z",
      end: "2026-09-02T12:00:00.000Z",
    },
  );
});

test("does not plan a sub-interval forward chunk while the latest window ages", () => {
  assert.equal(
    historyBuffer.planAdjacentChunk({
      range,
      loadedStart: "2026-09-01T08:00:00.000Z",
      loadedEnd: "2026-09-02T11:59:59.000Z",
      visibleStart: "2026-09-01T11:59:59.000Z",
      visibleEnd: "2026-09-02T11:59:59.000Z",
      now,
    }),
    null,
  );
});

test("merges adjacent points by timestamp without filling nullable fields", () => {
  const existing = [
    { timestamp: "2026-09-01T00:00:00Z", power_mw: 8, price_aud_per_mwh: 90, net_energy_value_aud: 1 },
    { timestamp: "2026-09-01T00:05:00Z", power_mw: null, price_aud_per_mwh: null, net_energy_value_aud: null },
  ];
  const incoming = [
    { timestamp: "2026-09-01T00:00:00.000Z", power_mw: null, price_aud_per_mwh: null, net_energy_value_aud: null },
    { timestamp: "2026-09-01T00:10:00Z", power_mw: 4, price_aud_per_mwh: 120, net_energy_value_aud: 2 },
  ];
  assert.equal(typeof historyBuffer.mergePoints, "function");
  assert.deepEqual(historyBuffer.mergePoints(existing, incoming), [
    { timestamp: "2026-09-01T00:00:00.000Z", power_mw: null, price_aud_per_mwh: null, net_energy_value_aud: null },
    existing[1],
    incoming[1],
  ]);
});

test("resolves and preserves absolute visible bounds from a dataZoom event", () => {
  assert.equal(typeof historyBuffer.resolveVisibleBounds, "function");
  assert.deepEqual(
    historyBuffer.resolveVisibleBounds(
      {
        batch: [{
          start: 0,
          end: 100,
          startValue: "2026-09-01T08:00:00Z",
          endValue: "2026-09-02T08:00:00Z",
        }],
      },
      { start: "2026-09-01T00:00:00Z", end: "2026-09-02T12:00:00Z" },
    ),
    {
      start: Date.parse("2026-09-01T08:00:00Z"),
      end: Date.parse("2026-09-02T08:00:00Z"),
    },
  );
});

test("retains only a bounded gutter around the visible window", () => {
  const points = Array.from({ length: 13 }, (_, index) => ({
    timestamp: new Date(Date.parse("2026-09-01T00:00:00Z") + index * 60 * 60 * 1000).toISOString(),
    power_mw: index,
    price_aud_per_mwh: null,
    net_energy_value_aud: null,
  }));
  assert.equal(typeof historyBuffer.retainPoints, "function");
  assert.deepEqual(
    historyBuffer.retainPoints(
      points,
      { start: "2026-09-01T05:00:00Z", end: "2026-09-01T07:00:00Z" },
      { beforeMs: 2 * 60 * 60 * 1000, afterMs: 2 * 60 * 60 * 1000 },
    ).map((point) => point.timestamp),
    points.slice(3, 10).map((point) => point.timestamp),
  );
});

test("does not plan adjacent pages for an explicitly bounded custom range", () => {
  assert.equal(
    historyBuffer.planAdjacentChunk({
      range: {
        preset: "custom",
        start: "2026-09-01T00:00:00.000Z",
        end: "2026-09-01T12:00:00.000Z",
      },
      loadedStart: "2026-09-01T00:00:00.000Z",
      loadedEnd: "2026-09-01T12:00:00.000Z",
      visibleStart: "2026-09-01T00:00:00.000Z",
      visibleEnd: "2026-09-01T12:00:00.000Z",
      now,
    }),
    null,
  );
});

test("retains requested coverage through the fetched end when the final point is earlier", () => {
  const requestedEnd = "2026-09-01T02:00:00.000Z";
  const retained = historyBuffer.retainBuffer(
    [
      { timestamp: "2026-09-01T00:00:00.000Z", power_mw: 1 },
      { timestamp: "2026-09-01T01:55:00.000Z", power_mw: 2 },
    ],
    { start: "2026-09-01T00:00:00.000Z", end: requestedEnd },
    { start: "2026-09-01T00:00:00.000Z", end: requestedEnd },
    { beforeMs: 60 * 60 * 1000, afterMs: 60 * 60 * 1000 },
  );
  assert.equal(retained.loadedEnd, Date.parse(requestedEnd));
});

test("plans a smaller initial historical gutter for a fixed preset", () => {
  assert.equal(typeof historyBuffer.initialGutterChunk, "function");
  assert.deepEqual(
    historyBuffer.initialGutterChunk(range, now),
    {
      direction: "previous",
      start: "2026-09-01T08:00:00.000Z",
      end: "2026-09-01T12:00:00.000Z",
    },
  );
});

test("plans an initial forward gutter for a historical fixed preset", () => {
  const historicalRange = {
    preset: "24h",
    start: "2026-09-01T10:00:00.000Z",
    end: "2026-09-02T10:00:00.000Z",
  };
  assert.equal(typeof historyBuffer.initialGutterChunks, "function");
  assert.deepEqual(
    historyBuffer.initialGutterChunks(historicalRange, now),
    [
      {
        direction: "previous",
        start: "2026-09-01T06:00:00.000Z",
        end: "2026-09-01T10:00:00.000Z",
      },
      {
        direction: "next",
        start: "2026-09-02T10:00:00.000Z",
        end: "2026-09-02T12:00:00.000Z",
      },
    ],
  );
});

test("does not plan a forward gutter for a latest fixed preset", () => {
  assert.deepEqual(historyBuffer.initialGutterChunks(range, now), [
    {
      direction: "previous",
      start: "2026-09-01T08:00:00.000Z",
      end: "2026-09-01T12:00:00.000Z",
    },
  ]);
});

test("does not plan a tiny forward gutter for a just-created latest range", () => {
  assert.deepEqual(
    historyBuffer.initialGutterChunks({
      preset: "24h",
      start: "2026-09-01T11:59:59.000Z",
      end: "2026-09-02T11:59:59.000Z",
    },
    now),
    [{
      direction: "previous",
      start: "2026-09-01T07:59:59.000Z",
      end: "2026-09-01T11:59:59.000Z",
    }],
  );
});

test("caps future loaded and visible bounds at the supplied current time", () => {
  const futureNow = "2026-09-02T12:00:00Z";
  const futurePage = historyBuffer.planAdjacentChunk({
    range,
    loadedStart: "2026-09-02T14:00:00Z",
    loadedEnd: "2026-09-02T16:00:00Z",
    visibleStart: "2026-09-02T14:00:00Z",
    visibleEnd: "2026-09-02T16:00:00Z",
    now: futureNow,
  });
  assert.ok(futurePage);
  assert.ok(Date.parse(futurePage.end) <= Date.parse(futureNow));
  assert.deepEqual(
    historyBuffer.resolveVisibleBounds(
      { startValue: "2026-09-02T14:00:00Z", endValue: "2026-09-02T16:00:00Z" },
      { start: "2026-09-02T14:00:00Z", end: "2026-09-02T16:00:00Z" },
      futureNow,
    ),
    { start: Date.parse(futureNow), end: Date.parse(futureNow) },
  );
});

test("keeps every adjacent request within the server's 30-day maximum", () => {
  const page = historyBuffer.planAdjacentChunk({
    range,
    loadedStart: "2026-09-01T12:00:00Z",
    loadedEnd: range.end,
    visibleStart: "2026-09-01T12:00:00Z",
    visibleEnd: "2026-09-02T12:00:00Z",
    now,
    chunkMs: historyBuffer.MAX_API_WINDOW_MS * 2,
  });
  assert.ok(page);
  assert.ok(Date.parse(page.end) - Date.parse(page.start) <= historyBuffer.MAX_API_WINDOW_MS);
});
