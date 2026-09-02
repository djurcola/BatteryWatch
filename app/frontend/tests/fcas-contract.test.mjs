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

test("FCAS panel exposes publication status and remains usable on mobile", () => {
  assert.match(source, /not yet public/);
  assert.doesNotMatch(source, /inactive/);
  assert.match(source, /role="status"/);
  assert.match(styles, /@media \(max-width: 780px\)[\s\S]*?\.fcas-panel/);
});
