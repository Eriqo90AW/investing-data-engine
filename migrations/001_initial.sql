CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE companies (
    id BIGSERIAL PRIMARY KEY,
    cik CHAR(10) UNIQUE,
    ticker TEXT UNIQUE,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX companies_ticker_lower_idx ON companies(lower(ticker)) WHERE ticker IS NOT NULL;
CREATE TABLE extraction_jobs (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id),
    requested_periods TEXT[] NOT NULL,
    refresh BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL CHECK (status IN ('queued','running','published','partial','failed')),
    progress JSONB NOT NULL DEFAULT '{}', errors JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ
);
CREATE INDEX extraction_jobs_active_idx ON extraction_jobs(company_id, created_at DESC) WHERE status IN ('queued','running');
CREATE TABLE published_datasets (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id),
    extraction_job_id BIGINT REFERENCES extraction_jobs(id),
    dataset_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staging','published','superseded')),
    source_checked_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    stale_after TIMESTAMPTZ NOT NULL,
    financials_payload JSONB NOT NULL,
    breakdowns_payload JSONB NOT NULL,
    UNIQUE(company_id, dataset_version)
);
CREATE INDEX published_datasets_current_idx ON published_datasets(company_id, published_at DESC) WHERE status='published';
CREATE TABLE facts (
    id BIGSERIAL PRIMARY KEY,
    dataset_id BIGINT NOT NULL REFERENCES published_datasets(id) ON DELETE CASCADE,
    statement TEXT NOT NULL, concept TEXT NOT NULL, label TEXT NOT NULL,
    value NUMERIC, unit TEXT, currency TEXT, scale TEXT, accounting_basis TEXT,
    period_start DATE, period_end DATE NOT NULL, period_kind TEXT NOT NULL,
    dimensions JSONB NOT NULL DEFAULT '{}', evidence JSONB NOT NULL DEFAULT '[]', formula JSONB, null_reason TEXT
);
CREATE INDEX facts_dataset_period_idx ON facts(dataset_id, period_end, statement);
CREATE TABLE reports (
    id BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id),
    cik CHAR(10), accession_number TEXT, form TEXT NOT NULL, filed_at DATE, period_end DATE,
    source_url TEXT NOT NULL, document_sha256 TEXT NOT NULL, storage_path TEXT NOT NULL,
    media_type TEXT NOT NULL, original_filename TEXT NOT NULL, parser_version TEXT,
    UNIQUE(company_id, document_sha256)
);
CREATE TABLE discovery_cache (
    company_id BIGINT NOT NULL REFERENCES companies(id), cache_key TEXT NOT NULL,
    payload JSONB, checked_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
    missing BOOLEAN NOT NULL DEFAULT FALSE, PRIMARY KEY(company_id, cache_key)
);
INSERT INTO schema_migrations(version) VALUES ('001_initial');


