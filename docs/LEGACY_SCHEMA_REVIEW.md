# Legacy Schema Review

Analysis of the **existing, problematic** FHAB database that currently produces the
published open data — from its PK/FK relationship diagram (`FHAB_PK_FK_relationships`).
The legacy DB was migrated from **Microsoft Access to SQL Server** (every table carries
an `SSMA_TimeStamp` — the SQL Server Migration Assistant marker), which explains much of
its shape. This review extracts what's useful for [SCHEMA_PROPOSAL.md](SCHEMA_PROPOSAL.md)
and flags the anti-patterns the modernization should fix.

## Legacy core tables

| Table | PK | Role |
|-------|----|----|
| `tbl_BloomReport` | `BloomReport_ID` | **Raw public submission** (intake form). No `Case_ID`. |
| `tbl_BloomInfo` | `BloomInfo_ID` | **Staff working bloom record** — has `Case_ID`, `BloomReport_CaseAssignment`, `PreviousAlgaeBloomReportID`, illness fields, attachments. The enriched bloom. |
| `tbl_CaseMngmt` | `Case_ID` | Case management (start/end/year, class, status, lead, notes). |
| `tbl_Response2` | `Response_ID` | **Overloaded hub** — links `Case_ID`, `BloomInfo_ID`, `Field_ID`, `Lab_ID`, `Monitoring_ID`, `Mitigation_ID`, `Advisory_ID`; mixes response, advisory, investigation, notification, communication, evidence. |
| `tbl_Advisory` | `Advisory_ID` | Advisory (start/end, recommended, posted, extent) — with `Case_ID`, `BloomInfo_ID`. |
| `tbl_FieldResults2` | `Field_ID` | Field measurements; FKs `Case_ID`, `BloomInfo_ID`. |
| `tbl_LabResults2` | `Lab_ID` | Lab analysis; FKs `Case_ID`, `BloomInfo_ID`; `AnalyteLevel1/2/3`, `Method`, `Taxa`, `COC_ID` (chain of custody), `Lab`. |

Plus many `tbl_BloomInfo_*` / `tbl_BloomReport_*` child tables for **multi-valued
attributes** (counties, textures, advisory types, illness types, rec-land managers,
waterbody managers, waterbody uses, attachments), and ~40 `LookUp_*` controlled
vocabularies.

## Key insights (what this changes / confirms)

### 1. The report→event split is real — and already in the data ⭐

The legacy DB already separates intake (`tbl_BloomReport`) from the staff working record
(`tbl_BloomInfo`). **`tbl_BloomInfo` ≈ our `event`**: it's the enriched bloom that carries
`Case_ID` and the case assignment. This independently validates the `report → event →
case` model.

> **Likely ID mapping to verify:** the published `bloom-report.csv` contains `Case_ID`,
> `Case_Class`, and advisory fields — columns that exist on **`tbl_BloomInfo`**, *not* on
> `tbl_BloomReport`. So the public file's **`Bloom_Report_ID` is very likely
> `BloomInfo_ID`** (the working record), and `tbl_BloomReport` is the raw intake form
> that feeds it. **This determines whether the public report key maps to our `event` or
> our `report`.** Confirm before finalizing the ID strategy — it's the single most
> important open item from this diagram.

### 2. Response relates to both event and case — confirmed

`tbl_Response2` carries both `Case_ID` **and** `BloomInfo_ID` (plus `Field_ID`, `Lab_ID`).
Our `response.event_id` + `response.case_id` design matches the real relationships.

### 3. Three-level analyte taxonomy — confirmed, with method dependencies

`LookUp_Lab_AnalyteLevel1`, `…Level1-2`, `…AnalyteLevel1-Method`, `…AnalyteLevel2-3-Method`
confirm `AnalyteLevel1/2/3` + `Method`, where valid methods depend on analyte-level
combinations. Our `analyte(analysis_type, analyte_class, analyte)` should add a
`method` relationship (valid method per analyte combo).

### 4. Field vs Lab results are physically separate

Legacy keeps `tbl_FieldResults2` and `tbl_LabResults2` apart; the published
`hab-results.csv` merges them. Our unified `sample`+`result` (discriminated by
`data_type`) is a deliberate simplification — keep it, but preserve the distinguishing
fields each side needs (lab: `COC_ID`, `Lab`, `AnalyteLevel*`; field: `FieldCrewLead`,
`FieldAgency`, `SampleStatus`, `ProposedLabAnalyses`).

## Elements to adopt that our proposal was missing

- **Personnel directory** (`LookUp_Personnel`: name, code, email, role, agency, region,
  DDW district) — `case_lead`, `response_update_by`, `field_crew_lead` should reference
  staff, not be free text.
- **Illness reporting** (`IllnessType`, `IllnessDescription` on report/event) — public-
  health relevant ("suspected illness reported"). Add to `report`/`event`.
- **Chain of custody** (`COC_ID`) and **lab identity** (`Lab`) on samples/results.
- **Management organizations & jurisdictions** (`LookUp_ManagementOrgs`, `WaterBodyManager`,
  `RecLandManager`) as a referenced org table, not repeated text.
- **Monitoring & Mitigation** as first-class response-linked concepts (`Monitoring_ID`,
  `Mitigation_ID`) — routine monitoring and mitigation actions, beyond advisory.
- **Notification / communication tracking** (`ResponseCommunicationParty/Tasks`,
  `NotificationType`, `NotifiedAgencies`) — supports the alert workflow (`DIS-4`).
- **DDW (Division of Drinking Water) districts/regions** and **Water Board regions** as
  reference tables.
- **Multi-valued attributes** (waterbody uses, bloom textures, counties, attachments) —
  genuinely many-to-many; model as junction tables, not delimited text.
- **Controlled-vocabulary / lookup strategy** — the ~40 `LookUp_*` tables map to
  staff-editable reference tables (satisfies `MGT-5`: expandable model matching the user
  vocabulary). Most should be reference tables, not hard-coded enums.
- **`EndDate` on lookups** — the legacy pattern retires a vocabulary value without
  deleting it (soft-expire). Worth keeping for reference tables.

## Anti-patterns to fix (why the legacy DB is "problematic")

1. **God-table `tbl_Response2`** — one table conflating response, advisory, investigation,
   notification, communication, and evidence. **Fix:** decompose into `response` +
   `advisory` + notification/communication tables (our proposal already splits advisory
   out; add the others).
2. **Duplicated advisory fields** — advisory data lives on *both* `tbl_Advisory` and
   `tbl_Response2` (StartDate, EndDate, AdvisoryRecommended, Extent, DisplayAdvisoryToMap…),
   inviting divergence. **Fix:** advisory lives in exactly one place (`advisory`),
   referenced by `response`.
3. **Report↔Info field duplication** — `tbl_BloomReport` and `tbl_BloomInfo` repeat ~40
   near-identical columns; triage copies data instead of referencing it. **Fix:** `event`
   references `report` and stores only what staff add/override; shared facts live once.
4. **Scattered parallel child tables** — a separate `tbl_*_X` per multi-valued attribute.
   **Fix:** consistent junction-table pattern (or Postgres arrays where appropriate).
5. **Access-origin denormalization** — flat, form-shaped tables. **Fix:** normalize to the
   lifecycle model; generate the flat files as exports (`DIS-2a`).

## Net effect on the proposal

The proposal's spine (`report → event → case`, `response` to both, advisory split out,
3-level analyte taxonomy) is **confirmed correct** by the legacy relationships. The
review adds: a personnel table, illness fields, chain-of-custody, management-org and
region reference tables, monitoring/mitigation + notification concepts, a lookup-table
strategy, and proper many-to-many modeling — and a clear list of legacy anti-patterns to
avoid. The one item to **verify with staff**: whether the public `Bloom_Report_ID` is
`BloomInfo_ID` (insight #1).
