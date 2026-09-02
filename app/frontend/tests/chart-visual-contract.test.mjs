import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const source = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");

test("dashboard content expands to the viewport instead of a narrow max-width", () => {
  assert.match(styles, /main\s*\{[^}]*max-width:\s*none/);
  assert.match(styles, /main\s*\{[^}]*width:\s*100%/);
});

test("chart panel fills remaining viewport height with a usable minimum", () => {
  assert.match(styles, /main\s*\{[^}]*min-height:\s*100vh/);
  assert.match(styles, /\.panel\s*\{[^}]*display:\s*flex/);
  assert.match(styles, /\.panel\s*\{[^}]*min-height:\s*560px/);
  assert.match(styles, /\.chart\s*\{[^}]*min-height:\s*480px/);
});

test("chart axes use high-contrast white labels", () => {
  assert.match(source, /axisLabel:\s*\{[^}]*color:\s*"#ffffff"/);
  assert.match(source, /nameTextStyle:\s*\{[^}]*color:\s*"#ffffff"/);
});

test("chart guides remain restrained while data series retain color", () => {
  assert.match(source, /splitLine:\s*\{[^}]*color:\s*"rgba\(255, 255, 255, 0\.1\)"/);
  assert.match(source, /lineStyle:\s*\{[^}]*color:\s*"rgba\(45, 212, 191, 0\.72\)"/);
  assert.match(source, /lineStyle:\s*\{[^}]*color:\s*"rgba\(251, 191, 36, 0\.55\)"/);
});

test("24-hour line series show circular points while longer ranges omit symbols", () => {
  assert.match(source, /name: "Battery Power \(MW\)"[\s\S]*?showSymbol: range\.preset === "24h"[\s\S]*?symbol: "circle"[\s\S]*?symbolSize: 7[\s\S]*?step: "start"/);
  assert.match(source, /name: "AEMO Price \(\$\/MWh\)"[\s\S]*?showSymbol: range\.preset === "24h"[\s\S]*?symbol: "circle"[\s\S]*?step: "start"/);
});

test("chart selector items include their measurement units", () => {
  assert.match(source, /data: \["Battery Power \(MW\)", "AEMO Price \(\$\/MWh\)", "Net Value \(\$\)"\]/);
});

test("tooltip labels include units and numeric values are limited to one decimal place", () => {
  assert.match(source, /formatter:\s*\(params/);
  assert.match(source, /Battery Power \(MW\)/);
  assert.match(source, /AEMO Price \(\$\/MWh\)/);
  assert.match(source, /Net Value \(\$\)/);
  assert.match(source, /toFixed\(1\)/);
});

test("tooltip includes a date and time for the hovered interval", () => {
  assert.match(source, /formatTimestamp/);
  assert.match(source, /Timestamp:/);
  assert.match(source, /Intl\.DateTimeFormat/);
});

test("tooltip gives emphasis only to the timestamp header", () => {
  assert.match(source, /<strong>Timestamp: \$\{formatTimestamp\(rawTimestamp\)\}<\/strong>/);
  assert.match(source, /fontWeight:\s*"normal"/);
  assert.match(source, /font-weight: normal/);
});

test("the fixed estimate identifies the selected preset period", () => {
  assert.match(source, /Estimated net value \(\{rangeLabel\(range\.preset\)\}\)/);
});

test("the dashboard exposes a net value for the currently visible chart range", () => {
  assert.match(source, /from "\.\/visible-net-value\.mjs"/);
  assert.match(source, /calculateVisibleNetValue/);
  assert.match(source, /chart\.on\("datazoom"/);
  assert.match(source, /Visible range net value/);
  assert.match(source, /setVisibleNetValue/);
});

test("mobile chart panels use the full viewport width", () => {
  assert.match(styles, /@media \(max-width: 780px\)[\s\S]*?main\s*\{[^}]*padding: 24px 0 40px/);
  assert.match(styles, /@media \(max-width: 780px\)[\s\S]*?\.panel\s*\{[^}]*border-left: 0/);
  assert.match(styles, /@media \(max-width: 780px\)[\s\S]*?\.panel\s*\{[^}]*border-right: 0/);
});

test("mobile charts default to power-only data with an opt-in price overlay", () => {
  assert.match(source, /const \[showPrice, setShowPrice\] = useState\(\(\) => !window\.matchMedia\("\(max-width: 780px\)"\)\.matches\)/);
  assert.match(source, /const visibleSeries = showPrice \? \[\{ show: true \}, \{ show: true \}, \{ show: true \}\] : \[\{ show: true \}, \{ show: false \}, \{ show: false \}\]/);
  assert.match(source, /grid: \{ left: isMobile \? 52 : 64, right: showPriceData \? 112 : 12/);
  assert.match(source, /legend: \{ data: showPriceData \? \["Battery Power \(MW\)", "AEMO Price \(\$\/MWh\)", "Net Value \(\$\)"\] : \["Battery Power \(MW\)"\]/);
});

test("crossing the mobile breakpoint reapplies responsive chart layout", () => {
  assert.match(source, /const applyResponsiveLayout = \(\) => \{[\s\S]*?const isMobile = mobileQuery\.matches/);
  assert.match(source, /mobileQuery\.addEventListener\("change", applyResponsiveLayout\)/);
  assert.match(source, /mobileQuery\.removeEventListener\("change", applyResponsiveLayout\)/);
});
