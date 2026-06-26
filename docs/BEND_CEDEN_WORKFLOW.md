# Workflow: Bend lab data → CEDEN 2.0 → FHAB events

**Status: recommendation / design.** How to get the Bend Genetics lab results (the
cyanotoxin and qPCR/genetic values that are *blank* in the FHAB `Measurement_Value` —
see [DATA_MODEL_CA_FHAB.md](DATA_MODEL_CA_FHAB.md)) into CEDEN 2.0 **and** connected to the
right FHAB event/case — both retrospectively and going forward.

## What already exists: the conversion tool (gap #1 — built)

The [`Bend_CEDEN_workflow`](https://github.com/ggearheart/Bend_CEDEN_workflow) R tool
already solves the **vocabulary/structure gap**. It takes Bend Genetics CSVs and emits
CEDEN 2.0 tables:

- `CEDEN_WaterChemistry_*` — **Chemistry_Results** (long format), columns include
  `#StationCode`, `CollectionDateTime` (MM/DD/YYYY HH:MM), `MatrixCode`, `MethodName`,
  `AnalyteName`, `FractionName`, `TestType`, `ResultTypeCode`, `Result`, `UnitName`,
  `ResQualCode`, `MDL`.
- `CEDEN_FieldResults_*` — **station visits / field metadata**.

It pivots wide→long, maps analytes/units/methods via `lookup/analyte_map.csv`, maps matrix
via `lookup/matrix_map.csv`, and handles non-detects (ND stored at the reporting limit with
`ResQualCode = "ND"`). Its CEDEN analyte vocabulary (which the FHAB DB should mirror):

| Class | CEDEN `AnalyteName` | Method | Matrix |
|-------|---------------------|--------|--------|
| Cyanotoxin (ELISA) | Microcystin, Anatoxin-a, Cylindrospermopsin, Saxitoxin | ELISA | water (µg/L), benthic (ng/g) |
| Genetic (qPCR) | mcyE gene, cyrA gene, sxtA gene, Anabaena circinalis 16S rRNA gene, Cyanobacteria 16S rRNA gene | qPCR | water/benthic (copies/mL, copies/g) |
| Pigment | Chlorophyll a, Pheophytin a | Spectrophotometry | water (µg/L) |

All with `FractionName = Total`. **Key mapping:** `StationCode = CustomerSample`, and the
Bend input carries a `SampleID` — these are the connectors for gap #2 (below).

## Confirmed output structure (from a real run)

The two CSVs the FHAB DB ingests (`src/fhab/ceden.py`):

- **`CEDEN_FieldResults`** — `StationCode, StationName, SampleDate, SampleTime, ProjectCode,
  MatrixName, SampleTypeCode, Collectors, EventCode, ProtocolCode, SampleComments`.
  **No coordinates** — `StationCode` (e.g. `201MUD500`) is a CEDEN/SWAMP station code;
  coordinates come from a CEDEN station registry.
- **`CEDEN_WaterChemistry`** — `StationCode, StationName, SampleDate, SampleTime,
  ProjectCode, LabAgencyCode, LabSampleID, LabBatch, BG_ID, Analyte, Result, ResQualCode,
  Units, Fraction, MDL, RL, MethodName, MatrixName, SampleTypeCode, QACode, ComplianceCode,
  LabSubmitDate, LabCompletionDate`. **`BG_ID`** (e.g. `WB6630`) is the per-sample key.

Note these are `SWB_RCMP_2026` **routine monitoring** samples — fixed-station data that
often has *no* corresponding FHAB bloom event. That validates the station-first design:
the data is valuable as monitoring/station results on its own; event linkage is opportunistic.

## What's left: ingest into FHAB + connect locations (gap #2)

The tool produces clean CEDEN-vocab output but, by design, **does no location matching** —
it assumes `StationCode` is pre-identified. So the FHAB database's job is:

1. **Ingest the tool's CEDEN output** (FieldResults + WaterChemistry) — *not* re-parse raw
   Bend. This **fills the blank `measurement_value`/`measurement_unit`** in FHAB results
   with the real cyanotoxin/qPCR numbers. ✅ implemented in `fhab.ceden.load_ceden_output`.
2. **Connect** each CEDEN station/sample to the correct FHAB **event/case** location, when
   one exists.

```
 Bend CSVs ──►  [ Bend_CEDEN_workflow (R) ]  ──►  CEDEN 2.0 Chemistry_Results + FieldResults
                  gap #1: vocab + structure            │
                                                        ▼
                                          [ FHAB DB loader (this repo) ]
                                          fill result values  +  connect to event/case
                                                        │  gap #2
                            ┌───────────────────────────┴───────────────────────────┐
                            ▼                                                         ▼
                  station registry (StationCode + geoconnex PID)        sample_link → event/case
```

## Connecting locations — the keys already exist

Because the Bend data carries `SampleID` and `CustomerSample`(→`StationCode`), the
connection leads with **deterministic keys**, not fuzzy matching:

| Tier | Rule | Confidence |
|------|------|-----------|
| 1 | `SampleID` / COC matches an FHAB `sample.sample_id` / `coc_id` | exact |
| 2 | `StationCode` (= CustomerSample) + `CollectionDateTime` matches a known station + FHAB sample date | high |
| 3 | **Spatial + temporal**: station point (from FieldResults coords) within *N* m of an FHAB event/location `ST_DWithin` **and** date within a window | scored |
| 4 | Waterbody / station-name fuzzy match | low |
| 5 | No confident match → **human review queue** | — |

Every link is written with method + confidence to a crosswalk (`sample_link`), never applied
silently — auditable and re-runnable.

### Canonical station registry — the durable spine

A `station` registry keyed by **`station_code`** (the CEDEN StationCode / CustomerSample)
and carrying a **geoconnex PID** ([GEOCONNEX.md](GEOCONNEX.md)) + PostGIS point + HUC-12 is
the shared identifier across Bend, CEDEN, and FHAB.

- **Going forward:** publish the registry so the Water Board field crew enters a known
  `StationCode` (= CustomerSample) that resolves straight to an FHAB station. The COC's
  `SampleID` ties the individual result to the FHAB sample. Connection by construction.
- **Retrospectively:** run the tiered matcher to seed `station` and `sample_link` from
  historical CEDEN output; work the review queue for the low-confidence tail.

## Schema additions (proposed, sketch DDL)

Builds on the implemented schema ([sql/schema.sql](../sql/schema.sql)). The FHAB DB
ingests **CEDEN-vocabulary** rows, so it does not need its own analyte crosswalk — the tool
owns that. It does need a station registry, a CEDEN ingest target, and link/result fields.

```sql
CREATE TABLE station (
    id            bigserial PRIMARY KEY,
    station_code  text UNIQUE,                 -- CEDEN StationCode (= Bend CustomerSample)
    station_name  text,
    waterbody_id  bigint REFERENCES waterbody(id),
    geom          geometry(Point, 4326),
    huc12         char(12) REFERENCES huc12(huc12),
    geoconnex_uri text UNIQUE
);
ALTER TABLE sample ADD COLUMN station_id bigint REFERENCES station(id);

-- CEDEN chemistry result fields the Bend tool emits but the FHAB result lacks today.
ALTER TABLE result ADD COLUMN res_qual_code text;   -- '=', 'ND', '<', …
ALTER TABLE result ADD COLUMN fraction_name text;   -- 'Total'
ALTER TABLE result ADD COLUMN mdl numeric;          -- method detection limit
-- analyte already has (analysis_type, analyte_class, analyte); align values to CEDEN
-- AnalyteName/MethodName so ingestion is a direct upsert.

-- Crosswalk: a CEDEN station/sample -> FHAB event/case, with how + how sure.
CREATE TABLE sample_link (
    id              bigserial PRIMARY KEY,
    sample_id       bigint REFERENCES sample(id),
    station_id      bigint REFERENCES station(id),
    bloom_report_id bigint REFERENCES event(bloom_report_id),
    case_id         bigint REFERENCES hab_case(case_id),
    match_method    text,                        -- sampleid | station_date | spatial_temporal | name | manual
    confidence      numeric,
    distance_m      numeric,
    reviewed_by     text,
    reviewed_at     timestamptz
);
```

## Proposed FHAB-side loader

`fhab.ceden.load_ceden_output(conn, chemistry_csv, field_csv)`:

1. **FieldResults → station** — get-or-create `station` per `StationCode`; set geometry
   from the visit coordinates; derive `huc12` (point-in-polygon) and mint the geoconnex PID.
2. **Chemistry_Results → sample + result** — per row: resolve `station`; get-or-create a
   `sample` (`station_id`, `sample_date` from `CollectionDateTime`, `coc_id`/`SampleID`);
   upsert the `analyte` by CEDEN `AnalyteName`/`MethodName`/matrix; insert the `result`
   with `Result`, `UnitName`, `ResQualCode`, `FractionName`, `MDL` — **filling the value**.
3. **Link** — write `sample_link` via the tiered matcher; confident links attach to the
   FHAB event/case, the rest to review.

This is idempotent (re-loading a CEDEN batch converges) and directly closes the blank-value
gap we found in the loaded FHAB data.

## Design principles

- **Reuse, don't duplicate.** The R tool owns Bend→CEDEN vocabulary; the FHAB DB consumes
  CEDEN vocabulary. One source of truth per concern.
- **Deterministic keys before probabilistic.** `SampleID` and `StationCode` first; spatial
  matching only fills gaps.
- **Persist provenance + confidence; never overwrite silently.** Re-runnable, auditable.
- **The station registry is the asset** — it serves Bend ingest, FHAB linkage, CEDEN
  submission, and the public map equally, with a persistent geoconnex identifier.

## Station registry & geocoding (built)

The **CEDEN station lookup** (`StationCode → TargetLatitude/Longitude`, 39,580 stations)
provides the coordinates FieldResults lacks. `scripts/fetch_ceden_stations.py` downloads and
parses it into `data/raw/ceden_stations.csv`; `fhab.ceden.load_station_registry` loads it
into the `station_registry` reference table; and `enrich_station_geom` sets `station.geom`
by `station_code`. Once geocoded, the linker's **spatial+temporal tier** connects a sample
to any FHAB event within 1 km and a 30-day window (verified: the Muddy Hollow Creek sample
linked to a seeded event at 27.8 m, confidence 0.97).

```bash
python scripts/fetch_ceden_stations.py
python scripts/init_db.py --ceden-stations data/raw/ceden_stations.csv \
                          --ceden FieldResults.csv WaterChemistry.csv
```

## Status

✅ **Built and tested** (`fhab.ceden`, `tests/test_ceden.py`, 9 tests): ingests the
FieldResults + WaterChemistry pair into `station` / `sample` / `result`, **filling the
analyte values**; geocodes stations from the CEDEN registry; and runs the tiered linker
(spatial+temporal → name). Verified end to end against a real run.

## Open questions / next steps

1. **Event linkage for routine data** — RCMP samples mostly won't match a bloom event;
   that's expected (they're retained as station/monitoring data and linked only when a bloom
   event coincides). Confirm that's the desired behavior.
2. **Tier-1 keying** — `BG_ID` is the per-sample key in the chemistry output. If FHAB field
   records captured the same `BG_ID`/`LabSampleID`, we can add a deterministic tier-1 exact
   match (the fields are already stored on `sample`). Does the FHAB side capture `BG_ID`?
3. **Datum normalization** — registry coordinates carry mixed datums (NAD83/WGS84/NR); for
   ~meter-level matching this is negligible, but worth normalizing if tolerances tighten.

## References

- The tool: <https://github.com/ggearheart/Bend_CEDEN_workflow> · <https://ggearheart.github.io/Bend_CEDEN_workflow/>
- CEDEN templates & lookup lists: <https://ceden.waterboards.ca.gov/data-templates.html>
- Related design: [GEOCONNEX.md](GEOCONNEX.md), [SCHEMA_PROPOSAL.md](SCHEMA_PROPOSAL.md)
