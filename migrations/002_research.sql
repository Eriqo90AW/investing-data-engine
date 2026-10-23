ALTER TABLE extraction_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'financials' CHECK (kind IN ('financials','market-data'));
ALTER TABLE extraction_jobs ADD COLUMN options JSONB NOT NULL DEFAULT '{}';
CREATE TABLE market_snapshots (
 id BIGSERIAL PRIMARY KEY, company_id BIGINT NOT NULL REFERENCES companies(id),
 extraction_job_id BIGINT REFERENCES extraction_jobs(id), dataset_version TEXT NOT NULL,
 source_checked_at TIMESTAMPTZ NOT NULL, published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 stale_after TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL, UNIQUE(company_id, dataset_version)
);
CREATE INDEX market_snapshots_latest_idx ON market_snapshots(company_id,published_at DESC);
