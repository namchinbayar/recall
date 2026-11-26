-- Hourly digest of events_raw, one row per hour and source, for `recall stream sink`.
-- Run both statements in a Confluent Cloud Flink SQL workspace (catalog: the environment,
-- database: the cluster). The INSERT keeps running; it costs one CFU while it does.

CREATE TABLE events_digest (
  window_start TIMESTAMP_LTZ(3) NOT NULL,
  window_end   TIMESTAMP_LTZ(3) NOT NULL,
  source       STRING NOT NULL,
  events       BIGINT NOT NULL,
  kinds        STRING,
  sample       STRING
) DISTRIBUTED INTO 1 BUCKETS WITH ('changelog.mode' = 'append');

INSERT INTO events_digest
SELECT window_start, window_end, source,
       COUNT(*) AS events,
       LISTAGG(DISTINCT COALESCE(kind, '-'), ', ') AS kinds,
       LISTAGG(SUBSTRING(REGEXP_REPLACE(COALESCE(body, ''), '\s+', ' ') FROM 1 FOR 140), ' | ') AS sample
FROM TABLE(TUMBLE(TABLE events_raw, DESCRIPTOR(occurred_at), INTERVAL '1' HOUR))
GROUP BY window_start, window_end, source;
