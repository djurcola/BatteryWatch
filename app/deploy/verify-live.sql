\pset tuples_only on
\pset format unaligned
SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';
SELECT count(*) FROM generators;
SELECT count(*) FROM generator_power_5m;
SELECT count(*) FROM dispatch_scada_artifacts;
SELECT count(*) FROM raw_dispatch_scada_observations;
SELECT max(report_timestamp) FROM dispatch_scada_artifacts;
SELECT max(interval_start) FROM generator_power_5m;
SELECT count(DISTINCT generator_id) FROM generator_power_5m;
