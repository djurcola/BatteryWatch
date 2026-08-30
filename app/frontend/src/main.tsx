import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import * as echarts from "echarts";
import "./styles.css";

type Generator = { duid: string; site_name: string; region: string };
type Point = { timestamp: string; power_mw: number; price_aud_per_mwh: number | null; net_energy_value_aud: number | null };
type Series = { generator: Generator; points: Point[]; summary: { exported_energy_mwh: number; imported_energy_mwh: number; net_energy_value_aud: number }; coverage: { price_coverage_percent: number; soc_coverage_percent: number }; estimate: { label: string; disclaimer: string } };

function money(value: number) { return new Intl.NumberFormat("en-AU", { style: "currency", currency: "AUD", maximumFractionDigits: 2 }).format(value); }
function number(value: number) { return `${value.toFixed(3)} MWh`; }
function recentWindow() {
  const end = new Date();
  const start = new Date(end.getTime() - 24 * 60 * 60 * 1000);
  return { start: start.toISOString(), end: end.toISOString() };
}

function App() {
  const [generators, setGenerators] = useState<Generator[]>([]);
  const [selected, setSelected] = useState("");
  const [series, setSeries] = useState<Series | null>(null);
  const [error, setError] = useState("");
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => { fetch("/api/generators").then(async (r) => { if (!r.ok) throw new Error(); return r.json(); }).then((body) => { if (!Array.isArray(body.generators) || body.generators.length === 0) { setError("No battery data is available yet."); return; } setGenerators(body.generators); setSelected(body.generators[0].duid); }).catch(() => setError("Unable to load generators.")); }, []);
  useEffect(() => {
    if (!selected) return;
    setError("");
    setSeries(null);
    const window = recentWindow();
    const query = new URLSearchParams({ generator: selected, start: window.start, end: window.end });
    fetch(`/api/series?${query}`).then(async (r) => { if (!r.ok) throw new Error(); return r.json(); }).then(setSeries).catch(() => setError("Unable to load the selected time range."));
  }, [selected]);
  useEffect(() => {
    if (!chartRef.current || !series) return;
    const chart = echarts.init(chartRef.current);
    chart.setOption({
      animation: false,
      tooltip: { trigger: "axis" },
      legend: { top: 8, data: ["Battery power", "AEMO price", "Net value"] },
      grid: { left: 58, right: 70, top: 48, bottom: 80 },
      xAxis: { type: "time" },
      yAxis: [{ type: "value", name: "Power MW", position: "left" }, { type: "value", name: "AUD/MWh", position: "right" }, { type: "value", name: "AUD", position: "right", offset: 52 }],
      dataZoom: [{ type: "inside", throttle: 50 }, { type: "slider", height: 24, bottom: 18 }],
      series: [
        { name: "Battery power", type: "line", showSymbol: false, yAxisIndex: 0, lineStyle: { width: 2, color: "#2dd4bf" }, areaStyle: { opacity: 0.12, color: "#2dd4bf" }, data: series.points.map((p) => [p.timestamp, p.power_mw]) },
        { name: "AEMO price", type: "line", showSymbol: false, yAxisIndex: 1, connectNulls: false, lineStyle: { width: 1.5, color: "#fbbf24" }, data: series.points.map((p) => [p.timestamp, p.price_aud_per_mwh]) },
        { name: "Net value", type: "bar", yAxisIndex: 2, itemStyle: { color: "#a78bfa" }, data: series.points.map((p) => [p.timestamp, p.net_energy_value_aud]) },
      ],
    });
    const resize = () => chart.resize(); window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); chart.dispose(); };
  }, [series]);

  return <main>
    <header><div><p className="eyebrow">STANDALONE BATTERY ANALYTICS</p><h1>BatteryWatch</h1><p className="lede">Five-minute battery power, regional price overlays, and transparent energy estimates.</p></div><label>Generator<select value={selected} onChange={(event) => setSelected(event.target.value)}>{generators.map((g) => <option key={g.duid} value={g.duid}>{g.site_name} · {g.duid} · {g.region}</option>)}</select></label></header>
    {error && <div className="error" role="alert">{error}</div>}
    {!series && !error && <div className="loading">Loading five-minute data…</div>}
    {series && series.points.length === 0 && <div className="loading">No observations in the selected 24-hour window.</div>}
    {series && series.points.length > 0 && <>
      <section className="cards"><article><span>Exported energy</span><strong>{number(series.summary.exported_energy_mwh)}</strong></article><article><span>Imported energy</span><strong>{number(series.summary.imported_energy_mwh)}</strong></article><article><span>Estimated net value</span><strong>{money(series.summary.net_energy_value_aud)}</strong></article><article><span>Data coverage</span><strong>{series.coverage.price_coverage_percent.toFixed(0)}% price</strong><small>{series.coverage.soc_coverage_percent.toFixed(0)}% SOC</small></article></section>
      <section className="panel"><div className="panel-heading"><div><h2>{series.generator.site_name}</h2><p>{series.points.length} five-minute intervals · zoom and pan enabled</p></div><span className="badge">{series.estimate.label}</span></div><div ref={chartRef} className="chart" /></section>
      <aside className="notice"><strong>Estimated value — not actual profit.</strong> {series.estimate.disclaimer}</aside>
    </>}
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
