# CA FHAB Published Data Model (reference)

The authoritative target model. Source: the State Water Resources Control Board's
**Surface Water — Freshwater Harmful Algal Blooms** dataset on the California Open Data
Portal, and its **HABs Master Data Dictionary**.

- Dataset: <https://data.ca.gov/dataset/surface-water-freshwater-harmful-algal-blooms>
- Data dictionary (PDF): `habs-master-data-dictionary.pdf`
- Disclaimer: `habs-disclaimer-for-data-dictionary.pdf`

The internal system is a **CRM / case-management** application used by Water Board and
partner state-agency staff. It tracks a suspected HAB through its whole life cycle:
intake → organize → investigate/respond → analyze → advise (list) → delist. The public
dataset publishes that internal data as four denormalized **flat files**.

## The four published flat files

Each flat file is denormalized — it repeats waterbody, location, and case context on
every row for self-contained analysis. Primary keys and the lifecycle relationships:

```
Report (Bloom_Report_ID)
   │  many reports grouped into…
   ▼
Case (Case_ID)
   ▲  staff actions reference report and/or case…
   │
Response (Response_Action_ID) ──issues──► Advisory (Advisory_ID)
   │
Result (Result_ID) ── field & lab analysis, references report and/or case
```

| File | Key | Rows* | Purpose |
|------|-----|-------|---------|
| `bloom-report.csv` (55 cols) | `Bloom_Report_ID` | ~3.6k | A suspected-bloom submission (public or partner). No determination is made at submission. |
| `hab-cases.csv` (52 cols) | `Case_ID` | ~1.4k | Staff organizational grouping of one or more reports for the same waterbody/source. |
| `hab-responses.csv` (42 cols) | `Response_Action_ID` | ~13k | Staff response actions, including advisory recommendations (list/update/delist). |
| `hab-results.csv` (47 cols) | `Result_ID` (+ `RESULT ID UNIQUE`, e.g. `F1`) | ~2.1k | Field and laboratory analysis results supporting/refuting a bloom. |

\* Approximate row counts from the 2026-06-02 snapshot.

### The lifecycle (CRM workflow)

1. **Report** — a suspected HAB is reported via the public form or by a partner.
   `Report_Type` records the source (e.g. "Public Reporting"). At this point it is
   neither confirmed nor denied. `Case_ID = 0` / `Case_Assignment = Unassigned` until
   staff triage it.
2. **Case** — staff create/assign a case to organize related reports for a waterbody.
   `Case_Class` (e.g. "Event Response"), `Case_Status` (Open/Closed), `Case_Lead`,
   `Case_Start_Date`/`Case_End_Date`, `Case_Year`. A waterbody can have multiple cases;
   a case can group multiple reports. Cases are an organizational tool, **not** a
   severity signal.
3. **Response** — staff act on a report/case. `Response_Category` / `Response_Type`
   (e.g. "Advisory"). An advisory action carries `Advisory_ID`, `Advisory_Recommended`
   ∈ {none, caution, warning, danger}, `Advisory_Start_Date`/`Advisory_End_Date`,
   `Spatial_Extent_of_Advisory`, and `DisplayAdvisoryToMap`. This is where a bloom is
   **listed** (advisory posted) or **delisted** (ended — bloom senesced, or it was
   never a HAB).
4. **Result** — field and lab analysis attached to a report/case as supplemental
   evidence (see analysis taxonomy below).

### Advisory model

Advisories live within responses. An advisory can be created, updated intra-/inter-
annually as new data arrives, and ended. Categories: **none / caution / warning / danger**.
`Display_Advisory_On_Map?` gates whether a record is posted to the CA HABs portal map
and open-data page (TRUE = approved for public display). For waterbodies not regularly
monitored there may be **no end date** (no confirmation a bloom has senesced).

### Results / analysis taxonomy

`hab-results.csv` carries both field observations and lab analysis. Key fields:
`Data_Type`, `Analysis_Type`, `Analyte_Class`, `Analyte`, `Method`, `Measurement_Type`,
`Measurement_Unit`, `Measurement_Value`, `Sample_Date`, `Sample_ID`, `Sample_Type`,
`Sample_Location`, `Site`, `Taxa`, `Results_Date`, `Proposed_Lab_Analyses`.

**`Data_Type` enum:** Laboratory, Veterinary, Field Visual, Field Measurement, Field
Batch, Lab Batch. ⚠️ **Veterinary data must not be published** (per the dictionary —
"Do not pull any veterinary data").

**Analysis taxonomy is three-level** (`AnalyteLevel1/2/3`):

| Level | Field | Examples |
|-------|-------|----------|
| 1 — Analysis Type | `Analysis_Type` | Cyanotoxin, Microscopy, Nutrient, Pigment |
| 2 — Analyte Class | `Analyte_Class` | microcystin, taxa dominance |
| 3 — Analyte | `Analyte` | total microcystin, total nitrogen, **mcyE** |

This covers the analysis kinds the program relies on:
- **Field visual / field measurement** — on-site observation and probe readings.
- **Microscopy** — taxa identification and dominance.
- **Genetic / molecular** — toxin-gene markers for toxin-producing cyanobacteria
  (e.g. **`mcyE`**, the microcystin-synthetase gene). Presence/absence or qPCR.
- **Cyanotoxin** — measured toxin concentrations (e.g. total microcystin, in µg/L).
- **Nutrient / pigment** — supporting water-quality context.

`Measurement_Value` may be numeric **or** categorical (e.g. presence/absence).

## Column inventories

<details><summary><strong>bloom-report.csv — 55 columns</strong></summary>

`Bloom_Report_ID`, `Case_Assignment`, `Case_ID`, `Number_of_Blooms_Linked_to_Case`,
`Report_Type`, `Bloom_Date_Created`, `Water_Body_Name`, `Official_Water_Body_Name`,
`Landmark`, `County`, `Regional_Water_Board`, `Bloom_Latitude`, `Bloom Longitude`,
`Bloom_Longitude`, `Bloom_Datum`, `Observation_Date`, `Has_Pictures`,
`Reported_Advisory_Types`, `Weather_Condition`, `Surface_Water_Condition`, `Bloom_Size`,
`Bloom_Location`, `Bloom_Texture`, `Reported_Management_Organizations`,
`DDW_District_Office`, `Drinking_Water_Source`, `Water_Body_Manager`, `Rec_Land_Manager`,
`Water_Body_Use`, `Water_Body_Type`, `Number_of_Advisories_Linked_to_Bloom`,
`Advisory_ID`, `Response_Update_By`, `Advisory_Recommended`, `Advisory_Date`,
`AdvisoryStartDate`, `AdvisoryEndDate`, `AdvisoryDetail`, `Advisory_Detail_Description`,
`Advisory_Date_of_Recommendation`, `Spatial_Extent_of_Advisory`, `Extent_Unit_of_Measure`,
`Last_Field_Result_Sample_Date`, `Lab_Data_Linked_to_Bloom`,
`Field_Visual_Records_Linked_to_Bloom`, `Field_Measurement_Data_Linked_to_Bloom`,
`Case_Start_Date`, `Case_Year`, `Case_Water_Body_Name`, `Case_Class`, `Case_Status`,
`Case_Lead`, `Case_End_Date`, `Case_DateTimeStamp`, `HAB_Bloom_Reports_FLATFILE_ID`.
</details>

<details><summary><strong>hab-cases.csv — 52 columns</strong></summary>

`Case_ID`, `Case_Start_Date`, `Case_Year`, `Case_Water_Body_Name`, `Case_Class`,
`Case_Status`, `Case_Lead`, `Case_End_Date`, `Case_Assignment`, `Bloom_Report_ID`,
`Number_of_Blooms_Linked_to_Case`, `Report_Type`, `Water_Body_Name`,
`Official_Water_Body_Name`, `Landmark`, `County`, `Regional_Water_Board`,
`Bloom_Latitude`, `Bloom_Longitude`, `Bloom_Datum`, `Observation_Date`, `Has_Pictures`,
`Number_of_Attachments_Linked_to_Bloom`, `Reported_Advisory_Types`, `Weather_Condition`,
`Surface_Water_Condition`, `Bloom_Size`, `Bloom_Location`, `Bloom_Texture`,
`Reported_Management_Organizations`, `DDW_District_Office`, `Drinking_Water_Source`,
`Water_Body_Manager`, `Rec_Land_Manager`, `Water_Body_Use`, `Water_Body_Type`,
`Number_of_Advisories_Linked_to_Case`, `Advisory_ID`, `Response_Update_By`,
`Advisory_Recommended`, `Advisory_Date`, `Advisory_Date_of_Recommendation`,
`Spatial_Extent_of_Advisory`, `Extent_Unit_of_Measure`, `Lab_Data_Linked_to_Case`,
`Field_Measurement_Data_Linked_to_Case`, `Display_Advisory_To_Map`, `Advisory_Start_Date`,
`Advisory_End_Date`, `Advisory_Detail`, `Field_Visual_Records_Linked_to_Case`,
`Case_DateTimeStamp`.
</details>

<details><summary><strong>hab-responses.csv — 42 columns</strong></summary>

`Response_Action_ID`, `Response_Category`, `Bloom_Report_ID`, `Case_ID`,
`Case_Assignment`, `Number_of_Blooms_Linked_to_Case`, `Report_Type`, `Water_Body_Name`,
`Official_Water_Body_Name`, `Landmark`, `County`, `Regional_Water_Board`,
`Bloom_Latitude`, `Bloom_Longitude`, `Bloom_Datum`, `Reported_Management_Organizations`,
`DDW_District_Office`, `Drinking_Water_Source`, `Water_Body_Manager`, `Rec_Land_Manager`,
`Water_Body_Use`, `Water_Body_Type`, `Response_Type`, `Advisory_ID`, `Advisory_Recommended`,
`Advisory_Date`, `DisplayAdvisoryToMap`, `Advisory_Start_Date`, `Advisory_End_Date`,
`Advisory_Detail`, `Advisory_Date_of_Recommendation`, `Spatial_Extent_of_Advisory`,
`Extent_Unit_of_Measure`, `Response_DateTimeStamp`, `Case_Start_Date`, `Case_Year`,
`Case_Water_Body_Name`, `Case_Class`, `Case_Status`, `Case_Lead`, `Case_End_Date`,
`Case_DateTimeStamp`.
</details>

<details><summary><strong>hab-results.csv — 47 columns</strong></summary>

`RESULT ID UNIQUE`, `Result_ID`, `Bloom_Report_ID`, `Case_Assignment`, `Case_ID`,
`Number_of_Blooms_Linked_to_Case`, `Report_Type`, `Water_Body_Name`,
`Official_Water_Body_Name`, `Landmark`, `County`, `Regional_Water_Board`,
`Bloom_Latitude`, `Bloom_Longitude`, `Bloom_Datum`, `Reported_Management_Organizations`,
`DDW_District_Office`, `Drinking_Water_Source`, `Water_Body_Manager`, `Rec_Land_Manager`,
`Water_Body_Use`, `Water_Body_Type`, `Bloom_Location`, `Bloom_Size`, `Bloom_Texture`,
`Bloom_Type`, `Datum`, `Latitude`, `Longitude`, `Measurement_Type`, `Measurement_Unit`,
`Measurement_Value`, `Proposed_Lab_Analyses`, `Results_Date`, `Sample_Date`, `Sample_ID`,
`Sample_Location`, `Data_Type`, `Site`, `Taxa`, `Water_Surface_Condition`,
`Weather_Condition`, `Analysis_Type`, `Method`, `Analyte_Class`, `Analyte`, `Sample_Type`.
</details>

## Notes for the modernized schema

- These flat files are **denormalized export views**, not the normalized internal model.
  The modernized DB should store a normalized core and *generate* these four files as
  exports (see [REQUIREMENTS.md](REQUIREMENTS.md) `DIS-2`).
- The four IDs — `Bloom_Report_ID`, `Case_ID`, `Response_Action_ID`, `Result_ID` (plus
  `Advisory_ID`) — are the stable join keys and must be preserved.
- The denormalized "linked to bloom/case" booleans and "number of … linked" counts are
  **derived** and should be computed at export time, not stored as source of truth.
- Veterinary results are collected but **excluded** from public exports.
