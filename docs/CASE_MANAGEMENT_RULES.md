# Case-Management Business Rules

Operational rules the interface and data validation must enforce, taken from the FHAB Data
System **User Manual** (case-management training). These complement the data model
([DATA_MODEL_CA_FHAB.md](DATA_MODEL_CA_FHAB.md)) and roles ([USER_ROLES.md](USER_ROLES.md)).

## Case rules

- A **case** is a folder; **reports** are the files in it (many reports per case). `Case_ID`
  is auto-generated.
- A case **cannot span more than one Regional Board**.
- **One case per waterbody** — unless the waterbody crosses a county, then one case per
  waterbody **+ county** (e.g. Gualala River across 3 counties → 3 cases).
- A case covers a **single calendar year** (`Case_Year`).
- **Status:** `Open` → `Ongoing` (long-running routine-monitoring cases) → `Closed`;
  may be `Re-opened`. A case is closeable once **all its reports have advisory = None**.

## Report / event rules

- **Bloom Info ID = Report ID = one site (lat/long) = one current advisory = one map dot.**
  (Confirms the model: the published `Bloom_Report_ID` keys the event, 1:1 with the report.)
- Original **visual bloom observations** submitted with the report form are **immutable**.
  Staff record updates by **adding** a *field visual assessment* (append-only history), not
  by editing the original — so a single report tracks change until the bloom subsides.
- `Report_Type` distinguishes **public** reports from **agency / routine-site** reports so
  staff can sort/prioritize.
- "… notes" fields are **private** (not published). **Only bold+underline fields display to
  the public** — the basis for withholding PII / illness / private notes from non-staff roles.

## Advisory model

- **Recommended Advisory** is a controlled list (richer than caution/warning/danger):
  `None`, `Caution`, `Warning`, `Danger`, `Algal mat alert sign`,
  `Algal mat general awareness sign`, `Visual observation`, `General awareness`,
  `NA - refer to Report Details`. (Planktonic CCHAB trigger levels + benthic algae-alert +
  general-awareness signage + visual-only observation.)
- **Advisory Detail** is a **controlled multi-select** lookup (~32 codes — e.g. "Under
  investigation", "Lake-wide advisory", "Suspected illness reported", "De-posting advisory")
  with public-facing description text. Multi-valued (hold Ctrl to pick several).
- **Advisory Start** = when the first advisory info is recorded (tracks how long staff
  worked the report); **End** = when the bloom subsides / is de-posted.
- **Advisory Recommended Date** = date the advisory action was determined.
- **`display_advisory_on_map`** gates public posting. Map symbology: caution / algae-alert →
  yellow circle; visual-observation → yellow square; absent-bloom → "refer to report
  details" symbol.

## Field & lab data

- **Field Data** — three entry paths: (1) *field visual assessment* (repeats the web-form
  visual observations), (2) *field measurement* (carries the **Sample ID** + air/water temp,
  turbidity, pH…), (3) *field batch* upload.
- **Lab Data** — three: (1) lab results (priority order: **toxins → taxonomy → qPCR →
  chl-a**), (2) **Illness Workgroup** tissue results, (3) lab batch upload.
- **Sample ID links** a field-collected sample to its lab results; enter **field data before
  lab** (the lab step then only needs the Sample ID).
- All field/lab data lives **in a case** and can be associated with **one report or the whole
  case**.
- Lab results are uploaded to **SWAMP/CEDEN independently** of this system — consistent with
  the [Bend→CEDEN→FHAB](BEND_CEDEN_WORKFLOW.md) design (the Bend tool feeds CEDEN; FHAB
  ingests separately).

## Water Body naming conventions

`Water_Body_Name` is free-text (submitter-entered, staff-verified) standardized for the
public dataset, with a paired `Landmark`. Summary: unnamed tributaries → "tributary to X" +
landmark "unnamed creek near X"; resource-area channels → resource-area name; golf-course /
park waterbodies → course / park name; marina → river/lake name + "at X marina"; coast →
"Pacific Ocean" (+ beach/cove landmark) or the bay/estuary name; confluence → larger
waterbody; barrier-separated areas → local public name (e.g. "Discovery Bay"). **Do not
publish lat/long on private property** — move to the nearest road/intersection and keep the
address in `Water_Body_Notes`.

## Model status vs. these rules

**Applied:** `case_status` and `advisory_recommended` are now text holding the full verbatim
controlled vocabulary (previously a 4-value enum silently dropped `Ongoing`/`Re-opened` and
the benthic/visual/general-awareness advisory types — ~1,669 advisories).

**Planned refinements** (not yet built):
- `advisory_detail` controlled **lookup table + multi-select junction** (legacy
  `LookUp_AdvisoryDetail` / `tbl_Advisory_AdvisoryDetail`).
- `recommended_advisory` and `case_status` **lookup tables** with display text + map symbology.
- Case business-rule **constraints/validation** (one region; one waterbody[+county]; single
  calendar year).
- Append-only **field visual assessment** history distinct from the original report observations.
