-- Esquema inicial (idempotente -- seguro re-ejecutar si ya corriste este script antes).
-- Equivalente en SQL plano de app/db/migrations/versions/0001_initial_schema.py

BEGIN;

CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

DO $$ BEGIN
    CREATE TYPE prospect_category AS ENUM ('distributor', 'retailer', 'installer_large', 'installer_independent', 'maintenance', 'refrigeration', 'competitor', 'other');
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE commercial_potential_level AS ENUM ('low', 'medium', 'high', 'very_high');
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE prospect_status AS ENUM ('new', 'enriched', 'reviewed', 'approved', 'rejected', 'synced');
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE dedup_status AS ENUM ('unique', 'needs_review', 'merged');
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE crm_sync_status AS ENUM ('not_synced', 'pending', 'synced', 'error');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS prospects (
    id UUID NOT NULL,
    name TEXT NOT NULL,
    trade_name TEXT,
    legal_form VARCHAR(20),
    rut VARCHAR(12),
    category prospect_category,
    specialties TEXT[],
    region TEXT,
    comuna TEXT,
    city TEXT,
    address TEXT,
    address_normalized TEXT,
    phone VARCHAR(20),
    phone_raw TEXT,
    email TEXT,
    website TEXT,
    social_media JSONB,
    google_place_id TEXT,
    google_rating FLOAT,
    google_ratings_total INTEGER,
    google_maps_url TEXT,
    employee_count_estimate TEXT,
    number_of_locations INTEGER,
    commercial_potential_score NUMERIC(5, 2),
    commercial_potential_level commercial_potential_level,
    scoring_breakdown JSONB,
    status prospect_status DEFAULT 'new' NOT NULL,
    dedup_status dedup_status DEFAULT 'unique' NOT NULL,
    merged_into_id UUID,
    crm_id TEXT,
    crm_sync_status crm_sync_status DEFAULT 'not_synced' NOT NULL,
    crm_last_synced_at TIMESTAMP WITH TIME ZONE,
    crm_sync_error TEXT,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    created_by TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY(merged_into_id) REFERENCES prospects (id),
    UNIQUE (rut),
    UNIQUE (google_place_id)
);

CREATE INDEX IF NOT EXISTS ix_prospects_region ON prospects (region);
CREATE INDEX IF NOT EXISTS ix_prospects_comuna ON prospects (comuna);
CREATE INDEX IF NOT EXISTS ix_prospects_category ON prospects (category);
CREATE INDEX IF NOT EXISTS ix_prospects_status ON prospects (status);
CREATE INDEX IF NOT EXISTS ix_prospects_dedup_status ON prospects (dedup_status);
CREATE INDEX IF NOT EXISTS ix_prospects_crm_sync_status ON prospects (crm_sync_status);
CREATE INDEX IF NOT EXISTS ix_prospects_potential_score ON prospects (commercial_potential_score);

DO $$ BEGIN
    CREATE TYPE job_type AS ENUM ('region_category_search', 'excel_import', 'manual_search', 'enrichment_refresh', 'dedup_scan', 'crm_sync_batch');
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM ('queued', 'running', 'completed', 'failed', 'partial');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS research_jobs (
    id UUID NOT NULL,
    job_type job_type NOT NULL,
    parameters JSONB,
    status job_status DEFAULT 'queued' NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    triggered_by TEXT,
    stats JSONB,
    error_log TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_research_jobs_status ON research_jobs (status);
CREATE INDEX IF NOT EXISTS ix_research_jobs_job_type ON research_jobs (job_type);

DO $$ BEGIN
    CREATE TYPE source_type AS ENUM ('google_places', 'website_scrape', 'social_media', 'excel_import', 'manual_edit', 'llm_enrichment', 'crm');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS prospect_sources (
    id UUID NOT NULL,
    prospect_id UUID NOT NULL,
    job_id UUID,
    source_type source_type NOT NULL,
    source_url TEXT,
    raw_data JSONB,
    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    fetched_by TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY(prospect_id) REFERENCES prospects (id),
    FOREIGN KEY(job_id) REFERENCES research_jobs (id)
);

CREATE INDEX IF NOT EXISTS ix_prospect_sources_prospect_id ON prospect_sources (prospect_id);
CREATE INDEX IF NOT EXISTS ix_prospect_sources_job_id ON prospect_sources (job_id);

DO $$ BEGIN
    CREATE TYPE dedup_candidate_status AS ENUM ('pending', 'merged', 'rejected_not_duplicate');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS dedup_candidates (
    id UUID NOT NULL,
    prospect_a_id UUID NOT NULL,
    prospect_b_id UUID NOT NULL,
    match_score FLOAT NOT NULL,
    match_reasons JSONB,
    status dedup_candidate_status DEFAULT 'pending' NOT NULL,
    reviewed_by TEXT,
    reviewed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(prospect_a_id) REFERENCES prospects (id),
    FOREIGN KEY(prospect_b_id) REFERENCES prospects (id),
    CONSTRAINT uq_dedup_pair UNIQUE (prospect_a_id, prospect_b_id)
);

CREATE INDEX IF NOT EXISTS ix_dedup_candidates_status ON dedup_candidates (status);

DO $$ BEGIN
    CREATE TYPE import_batch_status AS ENUM ('uploaded', 'processing', 'completed', 'failed');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS import_batches (
    id UUID NOT NULL,
    filename TEXT NOT NULL,
    uploaded_by TEXT,
    uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    column_mapping JSONB,
    row_count INTEGER,
    status import_batch_status DEFAULT 'uploaded' NOT NULL,
    storage_path TEXT,
    PRIMARY KEY (id)
);

DO $$ BEGIN
    CREATE TYPE crm_sync_direction AS ENUM ('search', 'upsert');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS crm_sync_log (
    id UUID NOT NULL,
    prospect_id UUID NOT NULL,
    attempted_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    direction crm_sync_direction NOT NULL,
    request_payload JSONB,
    response_payload JSONB,
    http_status INTEGER,
    success BOOLEAN DEFAULT false NOT NULL,
    error_message TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY(prospect_id) REFERENCES prospects (id)
);

CREATE INDEX IF NOT EXISTS ix_crm_sync_log_prospect_id ON crm_sync_log (prospect_id);

DO $$ BEGIN
    CREATE TYPE places_query_type AS ENUM ('text_search', 'nearby_search', 'place_details');
EXCEPTION WHEN duplicate_object THEN null; END $$;

DO $$ BEGIN
    CREATE TYPE places_field_tier AS ENUM ('pro', 'enterprise');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS google_maps_query_log (
    id UUID NOT NULL,
    query_type places_query_type NOT NULL,
    query_params JSONB,
    region TEXT,
    category TEXT,
    results_count INTEGER,
    field_mask_tier places_field_tier,
    cost_estimate_usd NUMERIC(8, 4),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_google_maps_query_log_created_at ON google_maps_query_log (created_at);

DO $$ BEGIN
    CREATE TYPE user_role AS ENUM ('admin', 'reviewer');
EXCEPTION WHEN duplicate_object THEN null; END $$;

CREATE TABLE IF NOT EXISTS users (
    id UUID NOT NULL,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role user_role DEFAULT 'reviewer' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS regions_comunas (
    id UUID NOT NULL,
    region TEXT NOT NULL,
    comuna TEXT NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_region_comuna UNIQUE (region, comuna)
);

INSERT INTO alembic_version (version_num) VALUES ('0001')
ON CONFLICT (version_num) DO NOTHING;

COMMIT;
