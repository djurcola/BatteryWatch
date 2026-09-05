import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

const labels = [
  "Region", "Asset Type", "Site Name", "Owner", "Technology Type", "Fuel Type",
  "DUID", "Status", "Nameplate Capacity (MW)", "Storage Capacity (MWh)",
  "Commercial Use Date", "Battery Manufacturer", "Battery Model",
  "Inverter Manufacturer", "Inverter Model", "Dispatch Type",
  "Status Bucket Summary", "Fuel Bucket Summary",
];

test("selected generator information uses existing generator state and all required labels", () => {
  assert.match(source, /type Generator = \{[\s\S]*?asset_type: string \| null/);
  assert.match(source, /const selectedGenerator = generators\.find\(\(generator\) => generator\.duid === selected\)/);
  assert.doesNotMatch(source, /fetch\(["']\/api\/generator-information/);
  for (const label of labels) assert.match(source, new RegExp(`<dt>${label.replace(/[()]/g, "\\$&")}</dt>`));
});

test("selected generator information renders null and blank values exactly as Not available", () => {
  assert.match(source, /function generatorInformationValue\(value: string \| number \| null \| undefined\)/);
  assert.match(source, /return value == null \|\| \(typeof value === "string" && value\.trim\(\) === ""\) \? "Not available"/);
  assert.match(source, /generatorInformationValue\(selectedGenerator\.owner\)/);
  assert.match(source, /generatorCapacity\(selectedGenerator\.capacity_mw, "MW"\)/);
  assert.match(source, /generatorCapacity\(selectedGenerator\.storage_capacity_mwh, "MWh"\)/);
});

test("generator information panel is responsive at the existing mobile breakpoint", () => {
  assert.match(source, /<section className="generator-information panel" aria-labelledby="generator-information-heading">/);
  assert.match(styles, /\.generator-information \{[\s\S]*?min-width: 0/);
  assert.match(styles, /@media \(max-width: 780px\)[\s\S]*?\.generator-information/);
});

test("generator information follows the power, price, and FCAS content", () => {
  const powerPriceChart = source.indexOf('<section className="panel"><div className="panel-heading">');
  const fcasPanel = source.indexOf('<section className="panel fcas-panel"');
  const generatorInformation = source.indexOf('<section className="generator-information panel"');

  assert.ok(powerPriceChart >= 0);
  assert.ok(fcasPanel > powerPriceChart);
  assert.ok(generatorInformation > fcasPanel);
});
