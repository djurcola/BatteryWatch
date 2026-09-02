import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import * as echarts from "echarts";
import "./styles.css";
import { calculateVisibleNetValue } from "./visible-net-value.mjs";
import {
  customRange,
  latestRange,
  rangeLabel,
  shiftRange,
  type HistoryRange,
  type RangePreset,
} from "./history-range.mjs";

type Generator = { duid: string; site_name: string; region: string };
type Point = { timestamp: string; power_mw: number | null; price_aud_per_mwh: number | null; net_energy_value_aud: number | null };
type Series = { generator: Generator; points: Point[]; summary: { exported_energy_mwh: number; imported_energy_mwh: number; net_energy_value_aud: number }; coverage: { power_coverage_percent: number; price_coverage_percent: number; soc_coverage_percent: number }; estimate: { label: string; disclaimer: string } };
type FcasServiceName = "raise_1s" | "lower_1s" | "raise_6s" | "lower_6s" | "raise_60s" | "lower_60s" | "raise_5m" | "lower_5m" | "raise_reg" | "lower_reg";
type FcasServicePoint = { target_mw: number | null; enablement_status: number | null; actual_availability_mw: number | null; enabled: boolean; trapped: boolean; stranded: boolean; cleared: boolean; participating: boolean; response_verified: false };
type FcasPoint = { timestamp: string; services: Record<FcasServiceName, FcasServicePoint> };
type FcasSummary = { reported_intervals: number; enabled_intervals: number; cleared_intervals: number; participating_intervals: number; trapped_intervals: number; stranded_intervals: number; max_target_mw: number | null; max_actual_availability_mw: number | null };
type Fcas = { selected_services: FcasServiceName[]; points: FcasPoint[]; coverage: { expected_intervals: number; observed_intervals: number; missing_intervals: number; coverage_percent: number }; latest_finalized: { interval_start: string; report_timestamp: string; downloaded_at: string; source_artifact_sha256: string; dispatch_interval: string; intervention: number; run_number: number } | null; publication_state: "available" | "partial" | "not_yet_public" | "no_data"; service_summaries: Partial<Record<FcasServiceName, FcasSummary>> };
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
function fcasServiceLabel(service: FcasServiceName) {
  const labels: Record<FcasServiceName, string> = { raise_1s: "Raise 1-second", lower_1s: "Lower 1-second", raise_6s: "Raise 6-second", lower_6s: "Lower 6-second", raise_60s: "Raise 60-second", lower_60s: "Lower 60-second", raise_5m: "Raise 5-minute", lower_5m: "Lower 5-minute", raise_reg: "Raise regulation", lower_reg: "Lower regulation" };
  return labels[service];
}
function fcasPublicationLabel(state: Fcas["publication_state"]) {
  if (state === "not_yet_public") return "not yet public";
  if (state === "no_data") return "no data";
  if (state === "partial") return "partial coverage";
  return "available";
}
const RANGE_OPTIONS: RangeOption[] = [
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
  { value: "custom", label: "Custom" },
];

function toLocalDateTimeInput(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function App() {
  const [generators, setGenerators] = useState<Generator[]>([]);
  const [selected, setSelected] = useState("");
  const [series, setSeries] = useState<Series | null>(null);
  const [fcas, setFcas] = useState<Fcas | null>(null);
  const [fcasError, setFcasError] = useState("");
  const [range, setRange] = useState<HistoryRange>(() => latestRange("24h"));
  const [visibleNetValue, setVisibleNetValue] = useState<number | null>(null);
  const [showPrice, setShowPrice] = useState(() => !window.matchMedia("(max-width: 780px)").matches);
  const [error, setError] = useState("");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const chartRef = useRef<HTMLDivElement>(null);
  const fcasChartRef = useRef<HTMLDivElement>(null);

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
    if (!selected) return;
    const controller = new AbortController();
    let obsolete = false;
    setFcas(null);
    setFcasError("");
    const query = new URLSearchParams({ generator: selected, start: range.start, end: range.end });
    fetch(`/api/fcas?${query.toString()}`, { signal: controller.signal })
      .then(async (r) => { if (!r.ok) throw new Error(); return r.json(); })
      .then((body) => { if (!obsolete) setFcas(body); })
      .catch((reason: unknown) => {
        if (!obsolete && !isAbortError(reason)) setFcasError(`Unable to load FCAS data for the selected ${rangeLabel(range.preset)} range.`);
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
  useEffect(() => {
    if (!fcasChartRef.current || !fcas || fcas.points.length === 0) return;
    const chart = echarts.init(fcasChartRef.current);
    const mobileQuery = window.matchMedia("(max-width: 780px)");
    const labels = fcas.selected_services.map(fcasServiceLabel);
    const applyResponsiveLayout = () => {
      chart.setOption({ grid: { left: mobileQuery.matches ? 52 : 64, right: 18, containLabel: true } });
    };
    chart.setOption({
      animation: false,
      aria: { enabled: true },
      tooltip: { trigger: "axis" },
      legend: { top: 8, textStyle: { color: "#ffffff" }, data: labels },
      grid: { left: 64, right: 18, top: 48, bottom: 58 },
      xAxis: { type: "time", axisLabel: { color: "#ffffff", fontSize: 11 } },
      yAxis: { type: "value", name: "Target MW", axisLabel: { color: "#ffffff", fontSize: 11 }, nameTextStyle: { color: "#ffffff" }, splitLine: { lineStyle: { color: "rgba(255, 255, 255, 0.1)" } } },
      series: fcas.selected_services.map((service) => ({
        name: fcasServiceLabel(service),
        type: "line",
        showSymbol: range.preset === "24h",
        symbol: "circle",
        symbolSize: 6,
        connectNulls: false,
        data: fcas.points.map((point) => [point.timestamp, point.services[service]?.target_mw ?? null]),
      })),
    });
    applyResponsiveLayout();
    const resize = () => { applyResponsiveLayout(); chart.resize(); };
    const mediaChange = () => resize();
    window.addEventListener("resize", resize);
    mobileQuery.addEventListener("change", mediaChange);
    return () => { window.removeEventListener("resize", resize); mobileQuery.removeEventListener("change", mediaChange); chart.dispose(); };
  }, [fcas, range.preset]);

  return <main>
    <header><div><p className="eyebrow">STANDALONE BATTERY ANALYTICS</p><h1>BatteryWatch</h1><p className="lede">Five-minute battery power, regional price overlays, and transparent energy estimates.</p></div><label>Generator<select value={selected} onChange={(event) => setSelected(event.target.value)}>{generators.map((g) => <option key={g.duid} value={g.duid}>{g.site_name} · {g.duid} · {g.region}</option>)}</select></label></header>
    <nav className="range-controls" aria-label="History range controls">
      <div className="range-presets" role="group" aria-label="Select history range">
        {RANGE_OPTIONS.map((option) => <button key={option.value} type="button" aria-pressed={range.preset === option.value} onClick={() => { if (option.value === "custom") { setCustomStart(toLocalDateTimeInput(range.start)); setCustomEnd(toLocalDateTimeInput(range.end)); setRange(customRange(range.start, range.end)); } else { setRange(latestRange(option.value)); } }}>{option.label}</button>)}
      </div>
      {range.preset !== "custom" && <div className="range-navigation" role="group" aria-label="Navigate selected range">
        <button type="button" aria-label="Previous selected range" onClick={() => setRange((current) => shiftRange(current, "previous"))}>Previous</button>
        <button type="button" aria-label="Next selected range" onClick={() => setRange((current) => shiftRange(current, "next"))}>Next</button>
        <button type="button" aria-label="Return to latest selected range" onClick={() => setRange((current) => latestRange(current.preset))}>Latest</button>
      </div>}
      {range.preset === "custom" && <div className="range-custom" role="group" aria-label="Custom date range">
        <label>Start<input type="datetime-local" value={customStart} onChange={(e) => setCustomStart(e.target.value)} /></label>
        <label>End<input type="datetime-local" value={customEnd} onChange={(e) => setCustomEnd(e.target.value)} /></label>
        <button type="button" onClick={() => { try { setRange(customRange(customStart, customEnd)); setError(""); } catch { setError("Custom range start must precede end."); } }}>Apply</button>
      </div>}
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
    {selected && <section className="panel fcas-panel" aria-labelledby="fcas-heading">
      <div className="panel-heading"><div><h2 id="fcas-heading">FCAS dispatch targets</h2><p>Selected {rangeLabel(range.preset)} window · grouped five-minute finalized service observations</p></div>{fcas && <span className={`badge fcas-state fcas-state-${fcas.publication_state}`} role="status" aria-live="polite">{fcasPublicationLabel(fcas.publication_state)}</span>}</div>
      {fcasError && <div className="error fcas-error" role="alert">{fcasError}</div>}
      {!fcas && !fcasError && <p className="loading" role="status" aria-live="polite">Loading FCAS dispatch targets for the selected range…</p>}
      {fcas && <>
        <p className="fcas-disclaimer"><strong>AEMO finalized dispatch target — not verified physical response</strong></p>
        <p className="fcas-coverage" role="status" aria-live="polite">{fcas.coverage.observed_intervals} of {fcas.coverage.expected_intervals} five-minute intervals reported · {fcas.coverage.coverage_percent.toFixed(0)}% coverage{fcas.latest_finalized ? ` · latest finalized ${formatTimestamp(fcas.latest_finalized.interval_start)}` : ""}</p>
        {fcas.points.length > 0 && <div ref={fcasChartRef} className="fcas-chart" role="img" aria-label="FCAS target MW time series" />}
        <div className="fcas-summary-grid">
          {fcas.selected_services.map((service) => { const summary = fcas.service_summaries[service]; return <article key={service}><h3>{fcasServiceLabel(service)}</h3>{summary ? <dl><div><dt>Reported</dt><dd>{summary.reported_intervals}</dd></div><div><dt>Enabled</dt><dd>{summary.enabled_intervals}</dd></div><div><dt>Cleared</dt><dd>{summary.cleared_intervals}</dd></div><div><dt>Participating</dt><dd>{summary.participating_intervals}</dd></div><div><dt>Trapped / stranded</dt><dd>{summary.trapped_intervals} / {summary.stranded_intervals}</dd></div><div><dt>Max target</dt><dd>{summary.max_target_mw == null ? "—" : `${summary.max_target_mw.toFixed(3)} MW`}</dd></div><div><dt>Max availability</dt><dd>{summary.max_actual_availability_mw == null ? "—" : `${summary.max_actual_availability_mw.toFixed(3)} MW`}</dd></div></dl> : <p>Summary unavailable.</p>}</article>; })}
        </div>
      </>}
    </section>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
