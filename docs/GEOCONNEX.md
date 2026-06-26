# Geoconnex Integration — Persistent Identifiers for HAB Locations

**Status: proposal / design.** This describes how the modernized FHAB database will mint
**persistent URL identifiers** for HAB event locations and monitoring sites using
[Geoconnex](https://geoconnex.us), the Internet of Water's national registry of
persistent identifiers (PIDs) for hydrologic features.

## Why geoconnex

The IoW Phase 1 framework ([REQUIREMENTS.md](REQUIREMENTS.md)) calls for *distributed
responsibilities* and *resilient data connections to public-facing maps*. Geoconnex is
the mechanism: each organization keeps its own data and landing pages, and geoconnex
provides a stable, dereferenceable web identifier (a "PID") for each feature so others
can link to it permanently — even if the underlying systems, map services, or URLs
change. A HAB event location gets one durable URL that public maps, partner datasets,
and the geoconnex knowledge graph can all point at.

## How geoconnex actually works (verified against the registry)

The [`internetofwater/geoconnex.us`](https://github.com/internetofwater/geoconnex.us)
repo is **just the PID registry**. Contribution is a pull request that adds a namespace
folder under `namespaces/`. A namespace folder contains:

| File | Purpose |
|------|---------|
| `*.csv` | One or more PID files. **Columns: `id,target`.** `id` is the persistent `https://geoconnex.us/...` URI; `target` is the URL it redirects to (the landing page). |
| `metadata.json` | `contact_email`, `dataset_description`, optional `skip_crawling`. |
| `README.md` | Human description: homepage, URL patterns, contacts. |

Example rows from the existing **`ca-gage-assessment`** namespace (CA Water Boards):

```csv
id,target
https://geoconnex.us/ca-gage-assessment,https://gispublic.waterboards.ca.gov/portal/home/item.html?id=32df...
https://geoconnex.us/ca-gage-assessment/gages/ABJ,https://sb19.linked-data.internetofwater.dev/collections/ca_gages/items/ABJ
```

- **PID URL pattern:** `https://geoconnex.us/{namespace}/{collection}/{local_id}`.
- A central **PID server** (separate infra; we don't run it) issues HTTP redirects from
  each `id` to its `target`.
- Unless `skip_crawling` is set, geoconnex **crawls the `target`** for JSON-LD /
  schema.org and indexes it into the knowledge graph (queryable via the geoconnex SPARQL
  endpoint). The CA gages example targets a **pygeoapi / OGC API – Features** item URL,
  which serves GeoJSON + JSON-LD out of the box.
- The registry contents are **public domain (CC0)**.

## Proposed design for FHAB

### 1. Namespace

Register a namespace — proposed **`ca-fhab`** — via PR to `geoconnex.us`, with two
collections:

| Collection | PID pattern | Feature |
|------------|-------------|---------|
| Monitoring sites (Tier 3 fixed stations) | `https://geoconnex.us/ca-fhab/sites/{site_id}` | A fixed monitoring location. |
| HAB event locations | `https://geoconnex.us/ca-fhab/events/{event_id}` | A confirmed/assessed bloom event location. |

(We mint PIDs for **stable** features — fixed sites and confirmed event locations — not
for every transient public report, which may be unverified or duplicated.)

### 2. Landing pages = OGC API – Features (pygeoapi)

Each PID `target` points at an OGC API – Features item served by **pygeoapi** over the
PostGIS database (the same pattern CA gages uses):

```
https://geoconnex.us/ca-fhab/events/4821   →   https://<our-host>/collections/fhab_events/items/4821
```

pygeoapi serves each feature as GeoJSON **and** JSON-LD (schema.org / GeoSPARQL), making
it crawlable for the knowledge graph with no extra landing-page code. This keeps the
"distributed responsibility" intact: we host and maintain our own features; geoconnex
only stores the redirect.

### 3. Store the PID in the database

The persistent URI is a first-class column on the location entities so it is the durable
public handle and survives map/service changes:

- `monitoring_site.geoconnex_uri`
- `hab_event.geoconnex_uri`

These are populated when a site/event is created and never reused.

### 4. Link to reference HUC features

Geoconnex publishes authoritative **reference features** (HUC watersheds, mainstems, NHD)
under its `ref/` namespace. Each FHAB location is related to the HUC-12 it falls within —
derived by **PostGIS point-in-polygon** against the **USGS Watershed Boundary Dataset
(WBD)** — and we store both the HUC-12 code and its geoconnex reference URI
(`https://geoconnex.us/ref/hu12/{huc12}`). This is what turns isolated points into
*resilient connections*: a HAB event is permanently tied to its watershed in the graph.

> **Open item:** confirm the authoritative HUC boundary source. Default assumption is the
> USGS WBD HUC-12 (the national standard, and geoconnex's own reference source). If you
> intended a specific California source, point me at it and I'll target that instead.

### 5. Generate the namespace CSV from the database

The `ca-fhab` PID CSV is a generated **export** (like the flat files): a query over
`monitoring_site` and `hab_event` emitting `id,target` rows, committed to a fork of
`geoconnex.us` and submitted as a PR. A scheduled job keeps it in sync as new fixed
sites and confirmed events are added.

## Workflow summary

```
PostGIS DB ──┬─► pygeoapi (OGC API Features)  ──► JSON-LD landing pages (targets)
             │
             └─► generate ca-fhab/*.csv  ──PR──► geoconnex.us registry ──► PID redirects
                                                                              │
        public maps & partners link to https://geoconnex.us/ca-fhab/... ◄─────┘
        each event also linked to https://geoconnex.us/ref/hu12/{huc12}
```

## Requirements added

See [REQUIREMENTS.md](REQUIREMENTS.md) `GEO-1..6`.

## References

- Geoconnex docs: <https://docs.geoconnex.us>
- Registry repo: <https://github.com/internetofwater/geoconnex.us>
- Contributing: <https://docs.geoconnex.us/contributing/overview>
- USGS Watershed Boundary Dataset (WBD): <https://www.usgs.gov/national-hydrography/watershed-boundary-dataset>
