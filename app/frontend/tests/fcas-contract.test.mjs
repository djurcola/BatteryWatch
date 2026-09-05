import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const html = await readFile(new URL("../index.html", import.meta.url), "utf8");

test("FCAS panel renders the API total estimate without browser-side arithmetic", () => {
  assert.match(source, /label: string; formula: string; payment_unit: "AUD"/);
  assert.match(source, /expected_service_intervals: number/);
  assert.match(source, /fcas\.estimate\.label/);
  assert.match(source, /fcas\.estimate\.estimated_payment_aud/);
  assert.doesNotMatch(source, /target_mw\s*\*\s*.*price_aud_per_mw_hour/);
});

test("FCAS estimates distinguish unavailable from a true zero and show coverage", () => {
  assert.match(source, /function fcasMoney\(value: number \| null\) \{ return value == null \|\| !Number\.isFinite\(value\) \? "Unavailable" : money\(value\); \}/);
  assert.match(source, /estimated_payment_aud: number \| null/);
  assert.match(source, /target \{coverageCount\(fcas\.estimate\.target_intervals, fcas\.estimate\.expected_service_intervals\)\}/);
  assert.match(source, /price \{coverageCount\(fcas\.estimate\.price_intervals, fcas\.estimate\.expected_service_intervals\)\}/);
  assert.match(source, /payment \{coverageCount\(fcas\.estimate\.payment_intervals, fcas\.estimate\.expected_service_intervals\)\} selected service-intervals/);
});

test("FCAS service cards and tooltip show API price and payment details", () => {
  assert.match(source, /<dt>Estimated payment<\/dt>/);
  assert.match(source, /<dt>Price \/ payment coverage<\/dt>/);
  assert.match(source, /price_aud_per_mw_hour/);
  assert.match(source, /estimated payment \$\{fcasMoney\(detail\.estimated_payment_aud\)\}/);
  assert.match(source, /tooltip: \{[\s\S]*?formatter:/);
});

test("FCAS estimate language includes the formula, latency, and exclusions", () => {
  assert.match(source, /Estimate formula: \{fcas\.estimate\.formula\}/);
  assert.doesNotMatch(source, /Estimate formula: target MW × regional FCAS price ÷ 12/);
  assert.doesNotMatch(source, /Publication may be delayed/);
  assert.match(source, /fcas\.estimate\.exclusions/);
});

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

test("FCAS service details default to expanded and contain only the service-card grid", () => {
  const chart = source.indexOf('ref={fcasChartRef}');
  const details = source.indexOf('<details className="fcas-details" open>');
  const grid = source.indexOf('<div className="fcas-summary-grid">');
  assert.ok(chart >= 0 && details > chart && grid > details);
  assert.match(source.slice(details, grid), /<summary>FCAS service details<\/summary>/);
  assert.match(source.slice(grid, source.indexOf('</details>', grid)), /fcas\.selected_services\.map/);
  assert.match(styles, /\.fcas-details summary::before/);
  assert.match(styles, /\.fcas-details\[open\] summary::before/);
  assert.match(styles, /\.fcas-details summary:focus-visible/);
});

test("FCAS graph, estimate context, and formula remain outside the collapsible details", () => {
  const details = source.indexOf('<details className="fcas-details" open>');
  for (const content of ['fcas-disclaimer', 'fcas-estimate', 'fcas-coverage', 'fcas-formula', 'ref={fcasChartRef}']) {
    assert.ok(source.indexOf(content) >= 0 && source.indexOf(content) < details, `${content} must precede the details control`);
  }
});

test("the local battery favicon is declared as SVG data", () => {
  assert.match(html, /<link rel="icon" href="data:image\/svg\+xml,/);
  assert.match(html, /%F0%9F%94%8B/);
});

test("FCAS target chart and service summaries remain in the panel", () => {
  assert.match(source, /echarts\.init\(fcasChartRef\.current\)/);
  assert.match(source, /target_mw/);
  assert.match(source, /fcas-state-\$\{fcas\.publication_state\}/);
});

test("FCAS panel exposes publication status and remains usable on mobile", () => {
  assert.match(source, /not yet public/);
  assert.doesNotMatch(source, /inactive/);
  assert.match(source, /role="status"/);
  assert.match(styles, /@media \(max-width: 780px\)[\s\S]*?\.fcas-panel/);
});
