-- FHAB Database Modernized — core schema
-- Target: SQLite (portable to PostgreSQL with minor type tweaks).
-- A normalized model for freshwater harmful algal bloom monitoring.

PRAGMA foreign_keys = ON;

-- A monitored waterbody (lake, reservoir, river reach, etc.).
CREATE TABLE IF NOT EXISTS waterbody (
    id              INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    waterbody_type  TEXT,                 -- lake | reservoir | river | pond | other
    county          TEXT,
    state           TEXT    DEFAULT 'CA',
    UNIQUE (name, county, state)
);

-- A physical monitoring location on a waterbody.
CREATE TABLE IF NOT EXISTS site (
    id            INTEGER PRIMARY KEY,
    waterbody_id  INTEGER NOT NULL REFERENCES waterbody(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    latitude      REAL,
    longitude     REAL,
    UNIQUE (waterbody_id, name)
);

-- A sampling event at a site on a given date.
CREATE TABLE IF NOT EXISTS sample (
    id            INTEGER PRIMARY KEY,
    site_id       INTEGER NOT NULL REFERENCES site(id) ON DELETE CASCADE,
    sample_date   TEXT    NOT NULL,        -- ISO-8601 (YYYY-MM-DD)
    collected_by  TEXT,
    source        TEXT,                    -- provenance: file or program name
    UNIQUE (site_id, sample_date)
);

-- A measured analyte result for a sample (e.g. microcystin, anatoxin-a, cell counts).
CREATE TABLE IF NOT EXISTS result (
    id          INTEGER PRIMARY KEY,
    sample_id   INTEGER NOT NULL REFERENCES sample(id) ON DELETE CASCADE,
    analyte     TEXT    NOT NULL,          -- e.g. 'microcystin'
    value       REAL,
    unit        TEXT,                      -- e.g. 'ug/L'
    detect_flag TEXT,                      -- detect | non-detect | estimated
    UNIQUE (sample_id, analyte)
);

-- A posted public-health advisory tied to a bloom event.
CREATE TABLE IF NOT EXISTS advisory (
    id            INTEGER PRIMARY KEY,
    waterbody_id  INTEGER NOT NULL REFERENCES waterbody(id) ON DELETE CASCADE,
    tier          TEXT    NOT NULL,        -- caution | warning | danger
    issued_date   TEXT    NOT NULL,
    lifted_date   TEXT,
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_site_waterbody  ON site(waterbody_id);
CREATE INDEX IF NOT EXISTS idx_sample_site     ON sample(site_id);
CREATE INDEX IF NOT EXISTS idx_result_sample   ON result(sample_id);
CREATE INDEX IF NOT EXISTS idx_advisory_wb     ON advisory(waterbody_id);
