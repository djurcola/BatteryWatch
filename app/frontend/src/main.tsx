import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import * as echarts from "echarts";
import "./styles.css";
import { calculateVisibleNetValue } from "./visible-net-value.mjs";
import {
  latestRange,
  rangeLabel,
  selectPreset,
  shiftRange,
  type HistoryRange,
  type RangePreset,
} from "./history-range.mjs";

type Generator = { duid: string; site_name: string; region: string };
type Point = { timestamp: string; power_mw: number | null; price_aud_per_mwh: number | null; net_energy_value_aud: number | null };
type Series = { generator: Generator; points: Point[]; summary: { exported_energy_mwh: number; imported_energy_mwh: number; net_energy_value_aud: number }; coverage: { power_coverage_percent: number; price_coverage_percent: number; soc_coverage_percent: number }; estimate: { label: string; disclaimer: string } };
type RangeOption = { value: RangePreset; label: string };
type TooltipEntry = { name: string; axisValue?: string | number; seriesName?: string; value: unknown; marker?: string };
type DataZoomEvent = { start?: number; end?: number; startValue?: unknown; endValue?: unknown; batch?: DataZoomEvent[] };

function money(value: number) { return new Intl.NumberFormat("en-AU", { style: "currency", currency: "AUD", maximumFractionDigits: 2 }).format(value); }
function number(value: number) { return `${value.toFixed(3)} MWh`; }
function formatTimestamp(value: unknown) {
  const date = new Date(typeof value === "number" ? value : String(value ?? ""));
  if (Number.isNaN(date.getTime())) return String(value ?? "");
  return new Intl.DateTimeFormat("en-AU", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}
function isAbortError(error: unknown) {
  return (error instanceof DOMException && error.name === "AbortError") || (error instanceof Error && error.name === "AbortError");
}

const RANGE_OPTIONS: RangeOption[] = [
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
];
function App() {
  const [generators, setGenerators] = useState<Generator[]>([]);
  const [selected, setSelected] = useState("");
  const [series, setSeries] = useState<Series | null>(null);
  const [range, setRange] = useState<HistoryRange>(() => latestRange("24h"));
  const [visibleNetValue, setVisibleNetValue] = useState<number | null>(null);
  const [showPrice, setShowPrice] = useState(() => !window.matchMedia("(max-width: 780px)").matches);
  const [error, setError] = useState("");
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    let obsolete = false;
    fetch("/api/generators", { signal: controller.signal })
      .then(async (r) => { if (!r.ok) throw new Error(); return r.json(); })
      .then((body) => {
        if (obsolete) return;
        if (!Array.isArray(body.generators) || body.generators.length === 0) {
          setError("No battery data is available yet.");
          return;
        }
        setGenerators(body.generators);
        setSelected(body.generators[0].duid);
      })
      .catch((reason: unknown) => {
        if (!obsolete && !isAbortError(reason)) setError("Unable to load generators.");
      });
    return () => { obsolete = true; controller.abort(); };
  }, []);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    let obsolete = false;
    setError("");
    setSeries(null);
    setVisibleNetValue(null);
    const query = new URLSearchParams({ generator: selected, start: range.start, end: range.end });
    fetch(`/api/series?${query.toString()}`, { signal: controller.signal })
      .then(async (r) => { if (!r.ok) throw new Error(); return r.json(); })
      .then((body) => { if (!obsolete) setSeries(body); })
      .catch((reason: unknown) => {
        if (!obsolete && !isAbortError(reason)) setError(`Unable to load the selected ${rangeLabel(range.preset)} range.`);
      });
    return () => { obsolete = true; controller.abort(); };
  }, [selected, range]);
  useEffect(() => {
    if (!chartRef.current || !series) return;
    const chart = echarts.init(chartRef.current);
    const mobileQuery = window.matchMedia("(max-width: 780px)");
    const handleDataZoom = (event: unknown) => {
      if (!event || typeof event !== "object") return;
      setVisibleNetValue(calculateVisibleNetValue(series.points, event as DataZoomEvent));
    };
    const applyResponsiveLayout = () => {
      const isMobile = mobileQuery.matches;
      const showPriceData = showPrice;
      const visibleSeries = showPrice ? [{ show: true }, { show: true }, { show: true }] : [{ show: true }, { show: false }, { show: false }];
      chart.setOption({
        legend: { data: showPriceData ? ["Battery Power (MW)", "AEMO Price ($/MWh)", "Net Value ($)"] : ["Battery Power (MW)"] },
        grid: { left: isMobile ? 52 : 64, right: showPriceData ? 112 : 12, containLabel: true },
        yAxis: [{ show: true }, { show: showPriceData }, { show: showPriceData }],
        series: visibleSeries,
      });
    };
    chart.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        backgroundColor: "#132238",
        borderColor: "rgba(255, 255, 255, 0.2)",
        textStyle: { color: "#ffffff", fontWeight: "normal" },
        formatter: (params: TooltipEntry | TooltipEntry[]) => {
          const entries = Array.isArray(params) ? params : [params];
          const firstEntry = entries[0];
          const rawTimestamp = firstEntry?.axisValue ?? (Array.isArray(firstEntry?.value) ? firstEntry.value[0] : firstEntry?.name);
          const title = `<strong>Timestamp: ${formatTimestamp(rawTimestamp)}</strong>`;
          const rows = entries.map((entry) => {
            const rawValue = Array.isArray(entry.value) ? entry.value[1] : entry.value;
            const value = typeof rawValue === "number" ? rawValue.toFixed(1) : rawValue == null ? "—" : String(rawValue);
            const label = entry.seriesName === "Battery Power (MW)" ? "Battery Power (MW)" : entry.seriesName === "AEMO Price ($/MWh)" ? "AEMO Price ($/MWh)" : "Net Value ($)";
            return `<span style="font-weight: normal">${typeof entry.marker === "string" ? entry.marker : ""}${label}: ${value}</span>`;
          });
          return [title, ...rows].join("<br/>");
        },
      },
      legend: { top: 8, textStyle: { color: "#ffffff" }, data: ["Battery Power (MW)", "AEMO Price ($/MWh)", "Net Value ($)"] },
      grid: { left: 64, right: 112, top: 48, bottom: 80 },
      xAxis: {
        type: "time",
        axisLabel: { color: "#ffffff", fontSize: 11 },
        axisLine: { lineStyle: { color: "rgba(255, 255, 255, 0.28)" } },
        axisTick: { lineStyle: { color: "rgba(255, 255, 255, 0.24)" } },
      },
      yAxis: [
        {
          type: "value",
          name: "Power MW",
          position: "left",
          axisLabel: { color: "#ffffff", fontSize: 11 },
          nameTextStyle: { color: "#ffffff" },
          axisLine: { lineStyle: { color: "rgba(255, 255, 255, 0.28)" } },
          axisTick: { lineStyle: { color: "rgba(255, 255, 255, 0.24)" } },
          splitLine: { lineStyle: { color: "rgba(255, 255, 255, 0.1)" } },
        },
        {
          type: "value",
          name: "AUD/MWh",
          position: "right",
          axisLabel: { color: "#ffffff", fontSize: 11 },
          nameTextStyle: { color: "#ffffff" },
          axisLine: { lineStyle: { color: "rgba(255, 255, 255, 0.28)" } },
          axisTick: { lineStyle: { color: "rgba(255, 255, 255, 0.24)" } },
          splitLine: { show: false },
        },
        {
          type: "value",
          name: "AUD",
          position: "right",
          offset: 52,
          axisLabel: { color: "#ffffff", fontSize: 11 },
          nameTextStyle: { color: "#ffffff" },
          axisLine: { lineStyle: { color: "rgba(255, 255, 255, 0.28)" } },
          axisTick: { lineStyle: { color: "rgba(255, 255, 255, 0.24)" } },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: "inside", start: 0, end: 100, throttle: 50 },
        {
          type: "slider",
          start: 0,
          end: 100,
          height: 24,
          bottom: 18,
          borderColor: "rgba(255, 255, 255, 0.18)",
          fillerColor: "rgba(255, 255, 255, 0.08)",
          handleStyle: { color: "#ffffff", opacity: 0.45 },
          textStyle: { color: "#ffffff" },
        },
      ],
      series: [
        {
          name: "Battery Power (MW)",
          type: "line",
          showSymbol: range.preset === "24h",
          symbol: "circle",
          symbolSize: 7,
          step: "start",
          yAxisIndex: 0,
          connectNulls: false,
          lineStyle: { width: 1.5, color: "rgba(45, 212, 191, 0.72)" },
          itemStyle: { color: "rgba(45, 212, 191, 0.9)" },
          areaStyle: { opacity: 0.04, color: "#2dd4bf" },
          data: series.points.map((p) => [p.timestamp, p.power_mw]),
        },
        {
          name: "AEMO Price ($/MWh)",
          type: "line",
          showSymbol: range.preset === "24h",
          symbol: "circle",
          symbolSize: 6,
          step: "start",
          yAxisIndex: 1,
          connectNulls: false,
          lineStyle: { width: 1, color: "rgba(251, 191, 36, 0.55)" },
          itemStyle: { color: "rgba(251, 191, 36, 0.82)" },
          data: series.points.map((p) => [p.timestamp, p.price_aud_per_mwh]),
        },
        {
          name: "Net Value ($)",
          type: "bar",
          yAxisIndex: 2,
          itemStyle: { color: "rgba(203, 213, 225, 0.16)" },
          data: series.points.map((p) => [p.timestamp, p.net_energy_value_aud]),
        },
      ],
    });
    chart.dispatchAction({ type: "dataZoom", start: 0, end: 100 });
    applyResponsiveLayout();
    setVisibleNetValue(series.summary.net_energy_value_aud);
    chart.on("datazoom", handleDataZoom);
    const resize = () => { applyResponsiveLayout(); chart.resize(); };
    const mediaChange = () => resize();
    window.addEventListener("resize", resize);
    mobileQuery.addEventListener("change", mediaChange);
    return () => { chart.off("datazoom", handleDataZoom); window.removeEventListener("resize", resize); mobileQuery.removeEventListener("change", mediaChange); chart.dispose(); };
  }, [series, showPrice, range.preset]);

  return <main>
    <header><div><p className="eyebrow">STANDALONE BATTERY ANALYTICS</p><h1>BatteryWatch</h1><p className="lede">Five-minute battery power, regional price overlays, and transparent energy estimates.</p></div><label>Generator<select value={selected} onChange={(event) => setSelected(event.target.value)}>{generators.map((g) => <option key={g.duid} value={g.duid}>{g.site_name} · {g.duid} · {g.region}</option>)}</select></label></header>
    <nav className="range-controls" aria-label="History range controls">
      <div className="range-presets" role="group" aria-label="Select history range">
        {RANGE_OPTIONS.map((option) => <button key={option.value} type="button" aria-pressed={range.preset === option.value} onClick={() => setRange(selectPreset(option.value))}>{option.label}</button>)}
      </div>
      <div className="range-navigation" role="group" aria-label="Navigate selected range">
        <button type="button" aria-label="Previous selected range" onClick={() => setRange((current) => shiftRange(current, "previous"))}>Previous</button>
        <button type="button" aria-label="Next selected range" onClick={() => setRange((current) => shiftRange(current, "next"))}>Next</button>
        <button type="button" aria-label="Return to latest selected range" onClick={() => setRange((current) => latestRange(current.preset))}>Latest</button>
      </div>
    </nav>
    <p className="selected-window" aria-live="polite">Selected {rangeLabel(range.preset)} window: {formatTimestamp(range.start)} – {formatTimestamp(range.end)}</p>
    {error && <div className="error" role="alert">{error}</div>}
    {!series && !error && <div className="loading">Loading five-minute data for the selected {rangeLabel(range.preset)} range…</div>}
    {series && series.points.length === 0 && <div className="loading">No observations in the selected {rangeLabel(range.preset)} window.</div>}
    {series && series.points.length > 0 && <>
      <section className="cards"><article><span>Exported energy</span><strong>{number(series.summary.exported_energy_mwh)}</strong></article><article><span>Imported energy</span><strong>{number(series.summary.imported_energy_mwh)}</strong></article><article><span>Estimated net value ({rangeLabel(range.preset)})</span><strong>{money(series.summary.net_energy_value_aud)}</strong></article><article><span>Visible range net value</span><strong>{money(visibleNetValue ?? series.summary.net_energy_value_aud)}</strong><small>Updates with chart zoom</small></article><article><span>Data coverage</span><strong>{series.coverage.power_coverage_percent.toFixed(0)}% power</strong><small>{series.coverage.price_coverage_percent.toFixed(0)}% price · {series.coverage.soc_coverage_percent.toFixed(0)}% SOC</small></article></section>
      <section className="panel"><div className="panel-heading"><div><h2>{series.generator.site_name}</h2><p>{series.points.length} five-minute intervals in the selected {rangeLabel(range.preset)} window · zoom and pan enabled</p></div><div className="panel-actions"><label className="chart-toggle"><input type="checkbox" checked={showPrice} onChange={(event) => setShowPrice(event.target.checked)} /> Show price data</label><span className="badge">{series.estimate.label}</span></div></div><div ref={chartRef} className="chart" /></section>
      <aside className="notice"><strong>Estimated value — not actual profit.</strong> {series.estimate.disclaimer}</aside>
    </>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
