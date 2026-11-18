import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import * as echarts from "echarts";
import "./styles.css";
import { calculateVisibleNetValue } from "./visible-net-value.mjs";

type Generator = { duid: string; site_name: string; region: string };
type Point = { timestamp: string; power_mw: number; price_aud_per_mwh: number | null; net_energy_value_aud: number | null };
type Series = { generator: Generator; points: Point[]; summary: { exported_energy_mwh: number; imported_energy_mwh: number; net_energy_value_aud: number }; coverage: { price_coverage_percent: number; soc_coverage_percent: number }; estimate: { label: string; disclaimer: string } };
type TooltipEntry = { name: string; axisValue?: string | number; seriesName?: string; value: unknown; marker?: string };
type DataZoomEvent = { start?: number; end?: number; startValue?: unknown; endValue?: unknown; batch?: DataZoomEvent[] };

function money(value: number) { return new Intl.NumberFormat("en-AU", { style: "currency", currency: "AUD", maximumFractionDigits: 2 }).format(value); }
function number(value: number) { return `${value.toFixed(3)} MWh`; }
function formatTimestamp(value: unknown) {
  const date = new Date(typeof value === "number" ? value : String(value ?? ""));
  if (Number.isNaN(date.getTime())) return String(value ?? "");
  return new Intl.DateTimeFormat("en-AU", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}
function recentWindow() {
  const end = new Date();
  const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
  return { start: start.toISOString(), end: end.toISOString() };
}
function App() {
  const [generators, setGenerators] = useState<Generator[]>([]);
  const [selected, setSelected] = useState("");
  const [series, setSeries] = useState<Series | null>(null);
  const [visibleNetValue, setVisibleNetValue] = useState<number | null>(null);
  const [showPrice, setShowPrice] = useState(() => !window.matchMedia("(max-width: 780px)").matches);
  const [error, setError] = useState("");
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => { fetch("/api/generators").then(async (r) => { if (!r.ok) throw new Error(); return r.json(); }).then((body) => { if (!Array.isArray(body.generators) || body.generators.length === 0) { setError("No battery data is available yet."); return; } setGenerators(body.generators); setSelected(body.generators[0].duid); }).catch(() => setError("Unable to load generators.")); }, []);
  useEffect(() => {
    if (!selected) return;
    setError("");
    setSeries(null);
    setVisibleNetValue(null);
    const window = recentWindow();
    const query = new URLSearchParams({ generator: selected, start: window.start, end: window.end });
    fetch(`/api/series?${query}`).then(async (r) => { if (!r.ok) throw new Error(); return r.json(); }).then(setSeries).catch(() => setError("Unable to load the selected time range."));
  }, [selected]);
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
        { type: "inside", throttle: 50 },
        {
          type: "slider",
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
          showSymbol: true,
          symbol: "circle",
          symbolSize: 7,
          step: "start",
          yAxisIndex: 0,
          lineStyle: { width: 1.5, color: "rgba(45, 212, 191, 0.72)" },
          itemStyle: { color: "rgba(45, 212, 191, 0.9)" },
          areaStyle: { opacity: 0.04, color: "#2dd4bf" },
          data: series.points.map((p) => [p.timestamp, p.power_mw]),
        },
        {
          name: "AEMO Price ($/MWh)",
          type: "line",
          showSymbol: true,
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
    applyResponsiveLayout();
    setVisibleNetValue(series.summary.net_energy_value_aud);
    chart.on("datazoom", handleDataZoom);
    const resize = () => { applyResponsiveLayout(); chart.resize(); };
    const mediaChange = () => resize();
    window.addEventListener("resize", resize);
    mobileQuery.addEventListener("change", mediaChange);
    return () => { chart.off("datazoom", handleDataZoom); window.removeEventListener("resize", resize); mobileQuery.removeEventListener("change", mediaChange); chart.dispose(); };
  }, [series, showPrice]);

  return <main>
    <header><div><p className="eyebrow">STANDALONE BATTERY ANALYTICS</p><h1>BatteryWatch</h1><p className="lede">Five-minute battery power, regional price overlays, and transparent energy estimates.</p></div><label>Generator<select value={selected} onChange={(event) => setSelected(event.target.value)}>{generators.map((g) => <option key={g.duid} value={g.duid}>{g.site_name} · {g.duid} · {g.region}</option>)}</select></label></header>
    {error && <div className="error" role="alert">{error}</div>}
    {!series && !error && <div className="loading">Loading five-minute data…</div>}
    {series && series.points.length === 0 && <div className="loading">No observations in the selected 24-hour window.</div>}
    {series && series.points.length > 0 && <>
      <section className="cards"><article><span>Exported energy</span><strong>{number(series.summary.exported_energy_mwh)}</strong></article><article><span>Imported energy</span><strong>{number(series.summary.imported_energy_mwh)}</strong></article><article><span>Estimated net value (preceding 24 hours)</span><strong>{money(series.summary.net_energy_value_aud)}</strong></article><article><span>Visible range net value</span><strong>{money(visibleNetValue ?? series.summary.net_energy_value_aud)}</strong><small>Updates with chart zoom</small></article><article><span>Data coverage</span><strong>{series.coverage.price_coverage_percent.toFixed(0)}% price</strong><small>{series.coverage.soc_coverage_percent.toFixed(0)}% SOC</small></article></section>
      <section className="panel"><div className="panel-heading"><div><h2>{series.generator.site_name}</h2><p>{series.points.length} five-minute intervals · zoom and pan enabled</p></div><div className="panel-actions"><label className="chart-toggle"><input type="checkbox" checked={showPrice} onChange={(event) => setShowPrice(event.target.checked)} /> Show price data</label><span className="badge">{series.estimate.label}</span></div></div><div ref={chartRef} className="chart" /></section>
      <aside className="notice"><strong>Estimated value — not actual profit.</strong> {series.estimate.disclaimer}</aside>
    </>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
