-- openAut POC2 — TimescaleDB schema for shunt control telemetry
-- Reuses the same 'openaut' schema as POC1. Run once against the POC1 database:
--   docker compose exec timescaledb psql -U openaut -d openaut -f /docker-entrypoint-initdb.d/shunt_init.sql
-- (or psql -U openaut -d openaut < db/shunt_init.sql)

CREATE SCHEMA IF NOT EXISTS openaut;

-- Generic readings table (mirrors POC1 modbus_readings shape so Telegraf can
-- ingest openaut/<site>/shunt/<signal> the same way).
CREATE TABLE IF NOT EXISTS openaut.shunt_readings (
    time         TIMESTAMPTZ      NOT NULL,
    site         TEXT             NOT NULL,
    signal_name  TEXT             NOT NULL,
    value        DOUBLE PRECISION,
    unit         TEXT,
    text_value   TEXT
);

SELECT create_hypertable('openaut.shunt_readings', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_shunt_readings_site_signal_time
    ON openaut.shunt_readings (site, signal_name, time DESC);

-- Convenience view: latest value per signal
CREATE OR REPLACE VIEW openaut.shunt_latest AS
SELECT DISTINCT ON (site, signal_name)
       site, signal_name, value, unit, text_value, time
FROM openaut.shunt_readings
ORDER BY site, signal_name, time DESC;
