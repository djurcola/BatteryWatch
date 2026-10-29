import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("FCAS panel uses the exact physical-response disclaimer", () => {
  assert.match(
    source,
    /AEMO finalized dispatch target — not verified physical response/,
  );
});

test("FCAS requests use selected bounds and abort obsolete responses", () => {
  assert.match(source, /\/api\/fcas/);
  assert.match(source, /new AbortController\(\)/);
  assert.match(source, /start: range\.start/);
  assert.match(source, /end: range\.end/);
  assert.match(source, /signal/);
  assert.match(source, /let obsolete = false/);
  assert.match(source, /if \(!obsolete\)/);
});

test("FCAS summaries expose enablement counts and availability maxima", () => {
  assert.match(source, /<dt>Enabled<\/dt>/);
  assert.match(source, /max_actual_availability_mw/);
});

test("FCAS selected-range lifecycle stays separate from endless energy paging", () => {
  const fcasRequest = source.indexOf("fetch(`/api/fcas?");
  assert.ok(fcasRequest >= 0);
  const fcasEffectStart = source.lastIndexOf("  useEffect(() => {", fcasRequest);
  const fcasEffectEnd = source.indexOf("\n  }, [selected, range]);", fcasRequest);
  assert.ok(fcasEffectStart >= 0);
  assert.ok(fcasEffectEnd > fcasRequest);
  const fcasEffect = source.slice(fcasEffectStart, fcasEffectEnd);
  assert.match(fcasEffect, /new AbortController\(\)/);
  assert.match(fcasEffect, /setFcas\(null\)/);
  assert.match(fcasEffect, /controller\.abort\(\)/);
  assert.doesNotMatch(fcasEffect, /fetchSeries|bufferRef|adjacentRequests|setSeries|setVisibleNetValue/);
  assert.match(source, /fetchSeries\(currentGenerator, chunk\.start, chunk\.end, controller\.signal\)/);
});

test("FCAS target chart and service summaries remain visible in the panel", () => {
  assert.match(source, /echarts\.init\(fcasChartRef\.current\)/);
  assert.match(source, /target_mw/);
  assert.match(source, /fcas\.selected_services\.map/);
  assert.match(source, /fcas-summary-grid/);
  assert.match(source, /fcas-state-\$\{fcas\.publication_state\}/);
});

test("FCAS panel exposes publication status and remains usable on mobile", () => {
  assert.match(source, /not yet public/);
  assert.doesNotMatch(source, /inactive/);
  assert.match(source, /role="status"/);
  assert.match(styles, /@media \(max-width: 780px\)[\s\S]*?\.fcas-panel/);
});
