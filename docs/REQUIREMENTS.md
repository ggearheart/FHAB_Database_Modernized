# Business Requirements — FHAB Data Ingestion

These requirements are derived from the **California Freshwater Harmful Algal Bloom
Monitoring Systems Data Ingestion Framework Recommendations — Phase 1: Model
Frameworks** (Internet of Water, in partnership with the California State Water
Resources Control Board / OIMA and The Commons).

The framework's purpose is to integrate **Tribal Government and community science
data** into California's modernized FHAB database for early warning, detection, and
monitoring. Its organizing principle is a **tiered collection model** that meets
contributors where they are — gathering the greatest amount of data of *known
quality* for the least contributor effort — followed by management (QAQC + API) and
dissemination (visualization, export, alerts).

Three core principles govern every system in the framework:

- **Organized around business capability** — software must produce structured,
  machine-readable data usable in contributors' own day-to-day work, not just the State's.
- **Cost-effective to maintain** — custom, COtS, or SaaS, it must be affordable and low-overhead.
- **Purposefully integrated** — must adhere to a structured data standard, export at
  minimum, and ideally expose a public/semi-public API. Many contributors already
  report to EPA's Water Quality Exchange (WQX); alignment with such standards is expected.

Requirement IDs are stable handles for traceability (`COL` = collection, `MGT` =
management, `DIS` = dissemination, `PRN` = principle).

---

## 1. Collection — the three-tier model

Contributors segment data collection into three tiers by the rigor they can support.
Ideally a program offers all three and targets each to the appropriate monitor.

### Tier 1 — Posts (`COL-T1`)

Low-training, high-volume observations: "eyes and ears" on a waterbody. Largely
unstructured but geolocated. **Flat** format — one row per observation, each with its
own lat/long point (not a station relationship).

| Field            | Type    | Collection method            |
|------------------|---------|------------------------------|
| `record_id`      | Integer | Machine generated            |
| `latitude`       | Decimal | Pulled from device           |
| `longitude`      | Decimal | Pulled from device           |
| `comments`       | String  | User entered                 |
| `image`          | String  | Captured via camera          |
| `watershed_huc`  | Integer | Automated based on location  |
| `watershed_name` | String  | Automated based on location  |
| `report_owner_fn`| Text    | User entered                 |
| `report_owner_ln`| Text    | User entered                 |
| `collection_date`| Date    | User entered                 |

- **`COL-T1.1`** Capture geolocation automatically from the device (lat/long).
- **`COL-T1.2`** Support image capture/attachment per observation.
- **`COL-T1.3`** Free-text `comments` as the only required qualifying field.
- **`COL-T1.4`** Auto-derive `watershed_huc` (HUC-12) and `watershed_name` from the
  point via a point-in-polygon lookup (e.g. PostGIS against national HUC-12 coverage).
- **`COL-T1.5`** Point geolocation may trigger ancillary functions (e.g. email with
  directions to a new observation, subscriber alerts).

### Tier 2 — Qualitative Sampling (`COL-T2`)

Extends Tier 1 with **contributor-defined custom parameters** for structured entry.
Still **flat** — each row is a record; custom attributes are appended as columns.

| Field                  | Type                                                          | Collection method           |
|------------------------|--------------------------------------------------------------|-----------------------------|
| `reading_id`           | Integer                                                      | Machine generated           |
| `latitude`             | Decimal                                                     | Pulled from device          |
| `longitude`            | Decimal                                                     | Pulled from device          |
| `watershed_huc`        | Integer                                                     | Automated based on location |
| `watershed_name`       | String                                                     | Automated based on location |
| `report_owner_fn`      | Text                                                        | User entered                |
| `report_owner_ln`      | Text                                                        | User entered                |
| `collection_date`      | Date                                                        | User entered                |
| *n appended parameters*| Text, Date, Enumeration, Integer, Decimal, Document, Image | User entered                |

- **`COL-T2.1`** Let a program administrator define its own schema: append *n*
  parameters, each with a name and one of the supported data types.
- **`COL-T2.2`** Supported parameter types: Text, Date, Enumeration, Integer,
  Decimal, Document, Image.
- **`COL-T2.3`** Support presence/absence-style observation workflows (e.g. new bloom
  sighting triggers follow-up sampling).

### Tier 3 — Quantitative Sampling (`COL-T3`)

**Relational / time-series.** Fixed monitoring sites with unique identifiers; many
readings over time belong to one site, enabling trend and seasonality analysis.
Requires the most data-management effort and typically a QAPP.

| Field                  | Type                                                          | Collection method           |
|------------------------|--------------------------------------------------------------|-----------------------------|
| `site_id`              | Integer                                                      | Machine generated           |
| `latitude`             | Decimal                                                     | Pulled from device          |
| `longitude`            | Decimal                                                     | Pulled from device          |
| `watershed_huc`        | Integer                                                     | Automated based on location |
| `watershed_name`       | String                                                     | Automated based on location |
| `site_name`            | Text                                                        | User entered                |
| `site_description`     | Text                                                        | User entered                |
| `reading_id`           | Integer                                                      | Machine generated           |
| `collection_date`      | Date                                                        | User entered                |
| *n appended parameters*| Text, Date, Enumeration, Integer, Decimal, Document, Image | User entered                |

- **`COL-T3.1`** Maintain fixed monitoring **sites** with unique IDs and metadata
  (`site_name`, `site_description`, location).
- **`COL-T3.2`** Model a one-to-many relationship: many `reading`s per `site` over time.
- **`COL-T3.3`** Same extensible appended-parameter mechanism as Tier 2 (`COL-T2.1/.2`).
- **`COL-T3.4`** Support program-level metadata (QAPP reference, protocols/methods, appropriate data uses).

---

## 2. Management

### QAQC & review status

- **`MGT-1`** QAQC rigor scales with tier. Tier 1 must allow **rapid ingestion** of
  self-categorized observations without an expert-review backlog blocking display.
- **`MGT-2`** Every public-facing post must show a **review status** set by a
  qualified individual (confirmation / status update / expert comment).
- **`MGT-3`** Contributors self-select a category at entry; an expert can validate later.
- **`MGT-4`** A **filter feature** must isolate data by tier/type so different agency
  decisions can rely on the appropriate confidence level.

### Data-management platform criteria

- **`MGT-5`** Expandable data model that matches the contributor's vocabulary while
  offering a high degree of standardization.
- **`MGT-6`** Cloud-hosted / universally accessible; redundancy and backups.
- **`MGT-7`** Contributors **retain ownership** of their own data.
- **`MGT-8`** Backed by an active developer community (where a platform is adopted).

### API

- **`MGT-9`** Expose a public or semi-public API that decouples the data model from
  applications and can map an internal schema to external standards.
- **`MGT-10`** Documented **GET** endpoints at a practical organizational unit; start
  small (Sites, Readings) and expand by user feedback.
- **`MGT-11`** Responses in **JSON**; support **authenticated** requests via API key.
- **`MGT-12`** Minimum exposed elements:
  - **Monitoring Sites** — station name, description, location.
  - **Readings** — parameter name, parameter method, sample result, unit of measure.
- **`MGT-13`** API may drive event triggers (e.g. a Tier 1 post fires a subscriber alert).

---

## 3. Dissemination

- **`DIS-1`** Interactive visualizations / dashboards / web maps communicating bloom
  status and associated **risk levels** (human health ranked the top data use).
- **`DIS-2`** **Export** in machine-readable formats; align with adopted standards
  (**CEDEN**, **WQX**) so authorized parties can always retrieve raw data.
- **`DIS-3`** Adopt **JSON-LD** for discoverability/indexing (search, voice assistants).
- **`DIS-4`** **Alerts** via email / SMS / push / social (e.g. Twilio, Mandrill),
  **only after** State, Tribal, and community authorities align on the public call to
  action; alerts must carry the recommended actions, framed for public-health context.

---

## 4. Governing principles (cross-cutting)

- **`PRN-1`** Organized around business capability — produce structured, machine-readable
  data useful to contributors' own workflows.
- **`PRN-2`** Cost-effective to build and maintain (custom / COtS / SaaS).
- **`PRN-3`** Purposefully integrated — adhere to a structured standard, export at
  minimum, prefer an API, and meet organizations where they are (any forward progress
  in machine-readability is acceptable).

---

## Source

*California Freshwater Harmful Algal Bloom Monitoring Systems Data Ingestion Framework
Recommendations — Phase 1.* Internet of Water, California State Water Resources Control
Board (OIMA), and The Commons. Tier field definitions: Tables 9–11. See
[GAP_ANALYSIS.md](GAP_ANALYSIS.md) for how the current schema maps to these requirements.
