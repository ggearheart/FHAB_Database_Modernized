# Workflow: Bend lab data → CEDEN 2.0 → FHAB events

**Status: recommendation / design.** How to ingest the Bend Genetics lab data (the
cyanotoxin and qPCR/genetic results that currently fill the *blank* `Measurement_Value`
fields in the FHAB results — see [DATA_MODEL_CA_FHAB.md](DATA_MODEL_CA_FHAB.md)) so it can
(1) be restructured for **CEDEN 2.0** (station + chemistry vocabulary) and (2) be
**connected to the right FHAB event/case location** — both retrospectively and going
forward.

## The problem in one picture

Three systems describe the same real-world thing (a sample collected at a place on a date)
but key it differently:

```
   BEND (lab values)              FHAB (case management)          CEDEN 2.0 (state exchange)
   ─────────────────              ──────────────────────          ─────────────────────────
   sample location, date     ?    event / case location           Station (StationCode)
   analyte, result, unit  ───┼──► result (value currently blank)   Chemistry (AnalyteName,
   COC / sample id            ?    sample (coc_id, sample_id)        Result, Unit, Method…)
```

The two gaps the workflow must close:
- **Vocabulary/structure gap** — Bend's analyte names, units, and methods must be mapped to
  CEDEN's controlled vocabulary and split into the CEDEN **Station** + **Chemistry**
  structure (CEDEN enforces this via its Data Checker; each Analyte+Matrix requires a
  specific Unit for comparability).
- **Location/identity gap** — a Bend *sampling location* must be tied to the FHAB
  *event/case location* so the lab values land on the correct bloom record.

## Core recommendation: a canonical **station** as the shared spine

Don't connect Bend↔FHAB point-to-point. Introduce one **canonical station registry** that
all three systems reference. This turns a fuzzy spatial-match problem into a deterministic
key join — *going forward*, and a one-time backfill *retrospectively*.

A `station` carries the identifiers each system needs:

- **`station_code`** — the CEDEN StationCode (the CEDEN join key).
- **`geoconnex_uri`** — the persistent web identifier ([GEOCONNEX.md](GEOCONNEX.md)); the
  durable cross-system handle that survives schema/URL changes.
- **`geom` / `huc12`** — PostGIS point + its watershed (point-in-polygon, `GEO-4`).
- **`waterbody_id`** — link to the FHAB waterbody.

Both a Bend sample and an FHAB event resolve to a `station`. The station is the hub:
*CEDEN chemistry rows hang off `station_code`; FHAB events/samples hang off the same
station; Bend results arrive carrying (ideally) the station and the chain-of-custody id.*

### Two keys do the connecting

1. **Station** (`station_code` / `geoconnex_uri`) — connects *locations*.
2. **Chain-of-custody / sample id** (`coc_id`, `sample_id`) — connects *individual
   samples/results*. Our `sample` table already has `coc_id` and `sample_id`; if Bend
   carries the same COC the field crew assigned, a Bend result joins an existing FHAB
   sample **exactly**, with no guessing.

## Retrospective vs. going forward

**Going forward (the durable fix).** Make the connection *by construction*:
- Publish the station registry to Bend and to FHAB field crews so everyone references the
  same `station_code` (and/or geoconnex PID) at sample time.
- Require Bend submissions to carry the **COC/sample id** that the FHAB field record uses.
- Then ingestion is a deterministic key join; no fuzzy matching needed.

**Retrospectively (the backfill).** Historical Bend data won't have clean keys, so run a
**tiered matcher** and persist every decision with provenance + confidence:

| Tier | Rule | Confidence |
|------|------|-----------|
| 1 | Exact `coc_id` / `sample_id` match to an FHAB sample | exact |
| 2 | Same `station_code` (if Bend already has one) | high |
| 3 | **Spatial + temporal**: Bend point within *N* m of an FHAB event/station (`ST_DWithin` on geography) **and** sample date within a window | scored by distance/date |
| 4 | Waterbody / station-name fuzzy match as tiebreaker | low |
| 5 | No confident match → **human review queue** | — |

Matches are written to a crosswalk table (not applied silently), so the process is
auditable, re-runnable, and improves as the review queue is worked.

## Schema additions (proposed, sketch DDL)

Builds on the implemented schema ([sql/schema.sql](../sql/schema.sql)).

```sql
-- Canonical monitoring station — the shared spine across Bend, FHAB, and CEDEN.
CREATE TABLE station (
    id            bigserial PRIMARY KEY,
    station_code  text UNIQUE,                 -- CEDEN StationCode
    station_name  text,
    waterbody_id  bigint REFERENCES waterbody(id),
    geom          geometry(Point, 4326),
    datum         text,
    huc12         char(12) REFERENCES huc12(huc12),
    geoconnex_uri text UNIQUE                   -- https://geoconnex.us/ca-fhab/sites/{id}
);
ALTER TABLE sample ADD COLUMN station_id bigint REFERENCES station(id);

-- Raw Bend submissions land here first (idempotent by batch + source row id).
CREATE TABLE bend_staging (
    id           bigserial PRIMARY KEY,
    batch_id     text,
    source_row   jsonb,                         -- preserve every original column
    loaded_at    timestamptz DEFAULT now()
);

-- Bend vocabulary -> CEDEN controlled vocabulary (curated; unmapped rows flagged).
CREATE TABLE analyte_crosswalk (
    id              bigserial PRIMARY KEY,
    source_system   text DEFAULT 'bend',
    source_analyte  text, source_unit text, source_method text,
    ceden_analyte   text, ceden_unit text, ceden_method text,
    ceden_matrix    text, ceden_fraction text,
    status          text DEFAULT 'pending',     -- pending | mapped | needs_review
    UNIQUE (source_system, source_analyte, source_unit, source_method)
);

-- Crosswalk: a Bend sample -> a station and (optionally) an FHAB event/case, with how.
CREATE TABLE sample_link (
    id              bigserial PRIMARY KEY,
    bend_staging_id bigint REFERENCES bend_staging(id),
    station_id      bigint REFERENCES station(id),
    bloom_report_id bigint REFERENCES event(bloom_report_id),
    case_id         bigint REFERENCES hab_case(case_id),
    match_method    text,                        -- coc | station | spatial_temporal | name | manual
    confidence      numeric,                     -- 0..1
    distance_m      numeric,
    reviewed_by     text,
    reviewed_at     timestamptz
);
```

## The pipeline (both directions)

```
Bend file ─► bend_staging ─► resolve location → station ─► map vocab → CEDEN
                                   │                              │
                                   ▼                              ▼
                       sample (station_id, coc_id) ─► result (value FILLED)
                                   │                              │
                 link sample → event/case (sample_link)     export Stations + Chemistry
                 (tiered matcher + review queue)             (CEDEN 2.0 vocab, validated)
```

1. **Stage** — load Bend files into `bend_staging` (raw JSON preserved; idempotent per
   batch). Never lose a source column.
2. **Resolve station** — match/insert the sampling location into `station` (COC → station
   → spatial). Assign `geoconnex_uri` + `huc12`.
3. **Map vocabulary** — apply `analyte_crosswalk`; route unmapped analyte/unit/method
   combos to curation. This is also where qPCR/genetic markers (`mcyE`, etc.) and
   cyanotoxins (microcystins, anatoxin-a, cylindrospermopsin, saxitoxin) get their CEDEN
   analyte/method codes — flag any that lack a clean CEDEN equivalent.
4. **Normalize into FHAB** — create `sample` (with `station_id`, `coc_id`) and `result`
   rows, **filling the previously-blank `measurement_value`/`measurement_unit`** with the
   real Bend values. This directly closes the gap we found in the loaded data.
5. **Connect to event/case** — write `sample_link` (tiered matcher); confident links
   attach the sample to its FHAB event/case; the rest go to the review queue.
6. **Export CEDEN 2.0** — emit a **Stations** file (`station_code`, name, lat/long,
   datum) and a **Chemistry** file (station_code, sample date, project/agency, matrix,
   method, analyte, fraction, result, qual code, unit) conforming to CEDEN 2.0 vocabulary;
   validate against CEDEN business rules / Data Checker before submission.

## Design principles (entity resolution)

- **Deterministic keys before probabilistic.** COC/sample id and `station_code` first;
  spatial/temporal/name matching only to fill gaps.
- **Persist provenance + confidence; never silently overwrite.** Every link records *how*
  and *how sure*; a human can audit and correct.
- **Idempotent and re-runnable.** Re-loading a batch or re-running the matcher converges,
  it doesn't duplicate.
- **Curate vocabulary once, reuse forever.** The crosswalk grows monotonically; mapped
  combos never need re-review.
- **Station registry is the asset.** Once built, it serves Bend ingest, FHAB linkage,
  CEDEN submission, and the public map equally.

## Open questions to confirm

1. **Does current Bend data carry the FHAB COC/`Sample_ID` or any `StationCode`?** If yes,
   most retrospective linking is a deterministic join and tiers 3–5 are a small tail. If
   no, spatial+temporal matching does the heavy lifting and the review queue matters more.
2. **CEDEN analyte coverage** — do Bend's cyanotoxin and qPCR analytes all have CEDEN
   controlled-vocabulary entries, or do some need new analyte/method codes coordinated
   with the CEDEN/SWAMP data center?
3. **Station authority** — is there an existing CEDEN station list for these waterbodies to
   seed the registry, or do we mint stations (and geoconnex PIDs) as we go?

## References

- CEDEN data templates & lookup lists: <https://ceden.waterboards.ca.gov/data-templates.html>,
  <https://ceden.org/ceden_namescodes.shtml>
- CEDEN chemistry results (published): <https://data.ca.gov/dataset/surface-water-chemistry-results-ceden-augmentation>
- Related design: [GEOCONNEX.md](GEOCONNEX.md) (persistent station identifiers),
  [SCHEMA_PROPOSAL.md](SCHEMA_PROPOSAL.md) (sample/result model).
