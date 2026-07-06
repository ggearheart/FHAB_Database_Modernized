# FHAB Database Modernized — Tester Onboarding Guide

**Audience:** program staff and community volunteers helping test the new FHAB system.
**Status:** pilot / testing. Please report anything confusing or broken (see [Giving feedback](#giving-feedback)).

This guide has two tracks — pick the one that matches your role:

- **[Community tester track](#track-a--community-tester)** — submit a bloom report from the
  CyanoSafe app, no login needed.
- **[Program staff track](#track-b--program-staff)** — log in to the FHAB Staff app to review
  reports, enter data, manage cases, and more.

> **Focus of this round — ingesting lab data.** The main thing we need tested is **getting
> cyanotoxin lab chemistry into FHAB and connected to the right event/report/case** — from lab
> **email folders**, **CEDEN** exports, and one-off spreadsheets. Staff testers: jump to
> [Lab data ingestion](#lab-data-ingestion--the-focus-of-this-round).

> **Important:** this is a **test system**. Do not treat anything here as an official advisory or
> an official report of record. Test data may be wiped at any time. For a real suspected bloom,
> still use the official channels.

---

## Systems and links

| System | What it is | Link |
|---|---|---|
| **CyanoSafe map (desktop)** | Public map of HAB data + "Report a Bloom" form | https://ggearheart.github.io/CyanoSafe_demo/ |
| **CyanoSafe app (phone/PWA)** | The mobile app most community users will use | https://ggearheart.github.io/CyanoSafe_phone_demo/ |
| **FHAB Staff app** | Staff workspace (login required) | https://fhab-web.onrender.com |

Your login (for staff testers) and any community-group API key will be provided to you
separately — they are not in this document.

---

## Track A — Community tester

You don't need an account. You're testing the **"Report a Bloom"** flow and confirming a
submission reaches staff.

### Submit a test bloom report (phone/PWA)

1. On your phone, open **https://ggearheart.github.io/CyanoSafe_phone_demo/**.
   *(Optional: tap your browser's "Add to Home Screen" to install it like an app.)*
2. Tap **Bloom List** (bottom), then expand **📋 Report a Bloom**.
3. *(Optional)* Tap **📷 Take / Choose Photo** — this also captures your GPS location.
4. Fill in at least the **Water body name** (required) and a location (your GPS, or pick a
   **County**). Add date, bloom size, and a short description if you can.
5. Tap **Submit report**. You should see **"Thank you — your report was received for review."**

> Please put **TEST** in the description (e.g. "TEST – ignore") so staff know it's a drill.

### What happens next

- Your report does **not** appear on the public map immediately. It goes into a **staff review
  queue**. A staffer reviews it and either promotes it to a tracked report or rejects it.
- If you reported a **suspected illness** (you or an animal got sick), that automatically alerts
  the program's illness team.

### What to test / look for

- Does the form submit successfully? Does the thank-you message appear?
- Does **📷 + location** work on your phone? Is the GPS roughly right?
- Try submitting with **only** a water body name + county (no GPS). Does it still work?
- Try an obviously bad entry (no water body name). Do you get a clear error?
- Is anything confusing, mislabeled, or hard to tap?

---

## Track B — Program staff

You'll log in to the **FHAB Staff app** at https://fhab-web.onrender.com.

### Roles (what you can see/do depends on your role)

| Role | Can do |
|---|---|
| **program_admin** | Everything, incl. account management and community-group API keys |
| **wb_staff** | Enter/edit reports, review submissions, cases, advisories, lab data |
| **field_staff** | Field verification + results entry |
| **lab_analyst** | Lab results / CEDEN data |
| **illness_workgroup** | Receives suspected-illness escalations |
| **ddw_staff** | Drinking-water focus |

Your data is **scoped to your Regional Water Board** — you generally see and manage your region's
records. (Cross-region report entry is allowed with a confirmation, for filing on behalf of
another board.)

### First login

1. Go to https://fhab-web.onrender.com and sign in with the credentials provided.
2. You land on the **Dashboard**: quick actions, and the reports you've recently worked on.
3. Note the **🔔 bell** (top right) — your notifications, including new submissions and illness
   alerts.

### Lab data ingestion — the focus of this round

The priority this round is **getting cyanotoxin lab chemistry into FHAB and connecting it to the
right event/report/case**. Lab results arrive in different shapes, so pick the path that matches
your file. Everything below is under the top-level **Ingest Data** menu (except attaching results
to a single report/case, which is on that report/case page).

| The lab data arrived as… | Use | Menu |
|---|---|---|
| A **folder of email attachments** from Bend/partner (results spreadsheet + CoC / transmittal / receipt PDFs) | **Ingest lab email folders** | Ingest Data → Ingest lab email folders |
| A **CEDEN WaterChemistry CSV** (Bend→CEDEN workflow output) | **Upload CEDEN lab data** | Ingest Data → Upload CEDEN lab data |
| A **full CEDEN chemistry template** (StationCode + date is the only link to an event) | **Lab batch reconciliation** | Ingest Data → Lab batch reconciliation |
| Results for **one already-known report/case** | **Attach on the report/case** | Reports / Cases → Lab upload |

**A. Email folder upload (Bend/partner) — the newest path.** Labs email a folder: a wide-format
results spreadsheet (analytes across columns) plus a scanned **chain-of-custody (CoC)**,
**transmittal letter**, and **sample-receipt form**.
1. **Ingest Data → Ingest lab email folders**.
2. Select **all** files from the one folder (results CSV/XLSX **and** the PDFs); add a **Source
   label** like `Clear Lake (RB5)`.
3. **Ingest folder** — you get counts (*N samples, M geocoded, K results, J files*) and land on
   the workboard filtered to that batch.
   - The spreadsheet is converted to CEDEN long form (ELISA toxins, dry-weight toxins, qPCR genes,
     chlorophyll; `ND` → non-detect). Samples come in **unlinked** and are **geocoded from the
     CEDEN station registry** when the station code is known.
   - The **source files stay on the batch** — `coc · data · receipt · transmittal` links (they
     open in a **new tab**). A folder with only PDFs still stores the files.
   - **Many folders at once:** the same screen has a **Many folders at once** box — pick a *parent*
     folder of many lab folders and **Ingest all subfolders**; each subfolder becomes its own batch,
     uploaded one at a time with progress (good for 50+).

**B. Upload CEDEN lab data** (`Ingest Data → Upload CEDEN lab data`) — for a CEDEN WaterChemistry
CSV (workflow output). Loads samples + results, geocodes from the registry, and **auto-links**
confident station+date matches.

**C. Lab batch reconciliation** (`Ingest Data → Lab batch reconciliation`) — for a full CEDEN
**template** you want to review before writing. The batch is **staged**; for each station+date
group the matcher suggests candidates — **Promote**, link manually, or **create a report**.
**Auto-link** accepts all confident, unambiguous matches at once.

**D. Attach to one report/case** — open the **report** (or **case**) and use its **Lab upload** to
pin a CEDEN CSV directly to that site.

**Reconcile on the Lab data workboard** (`Ingest Data → Lab data workboard`) — a task board to
connect samples to events/reports/cases and QA-review the links.
- **Status chips:** Unlinked / Linked·awaiting QA / Flagged / QA approved / **Routine sampling**,
  plus **Geocoded · not linked** (ready to research) and **Needs geocoding** (waiting on
  coordinates). **Filters:** status, assignee (incl. **Me** / Unassigned), region, a **Geocoding**
  filter, and a station/water-body search.
- **Assign:** check rows → pick a team member → **Assign**.
- **Per sample:** **Link/Re-link** by report (R#) or case (C#); **+Report**; **✓ QA**; **⚑** flag;
  **Unlink**; **Routine**. *Re-linking clears QA so it returns to review.* **Batch reconcile**
  auto-links the confident matches in the current view.
- **Sample map (🗺) — the research popup:** shows a **provenance summary** (how the sample was
  ingested + its lab IDs + result count + **source files** like the CoC, opening in a new tab, or
  *"no source files stored"*), the **map** (sample as a ★, nearby reports colored by time gap: blue
  ≤14 d, yellow 14–30 d, pink 30–60 d, gray >60 d), and a **list of the nearby reports** below it —
  check **one** to link to that report, or **several sharing a case** and **Link selected** to link
  to the case (handy when markers overlap).
- **Geocoding ungeocoded samples:** if a station isn't in the registry, its coordinates are on the
  **CoC**. Open the CoC (new tab), type the lat/long, **Search nearby**, then **Save to sample**.
  **Scan CoC (OCR)** tries to read them automatically (best-effort in the hosted pilot; if it says
  OCR isn't available, just type them). For a batch whose samples each have their **own** CoC
  coordinates, use its **Enter coords** screen (from the *Ingested batches* table or the workboard
  batch banner) to transcribe each sample's lat/long — each gets its own point. Or **Ingest Data →
  Bulk sample coordinates** to paste `station code, latitude, longitude` rows.
- **Routine sampling:** samples that aren't a bloom report/case — tag **Routine** to move them out
  of the unfinished queue; **↩ Unlinked** puts them back.

**Suggested lab-ingest test script (~15 min):** ingest an **email folder** and open its stored
**CoC**; on the workboard, **link** a geocoded sample to a nearby report (or **+Report**);
**geocode** an ungeocoded sample from its CoC coordinates; try **Bulk sample coordinates**; tag one
**Routine**; run **Reconcile all unlinked in this view**; then open **Lab** and confirm the results
and **Download CSV**.

### Other things to test

**1. Review the submission queue** (`Submissions`)
- New community/app reports land here as **pending**.
- For each: choose a **region** and **Promote** (creates a tracked report) or **Reject**.
- Promoting carries over everything the reporter sent (location, bloom details, photo, and any
  suspected-illness info) and starts the report as *under investigation*.
- Try the filters (pending / promoted / rejected) and the **trusted groups** filter.

**2. Enter a new report** (`New report`)
- This mirrors the official MyWaterQuality bloom form: report type, water body, county,
  landmark, coordinates, bloom characteristics (size, textures, weather, signs), reporter
  contact, suspected illness, and photos.
- **Water body** has a type-ahead: as you type, it suggests existing waterbodies — pick one to
  avoid creating duplicates. If you enter a near-duplicate name, you'll be asked to confirm.
- **County** is a dropdown (controlled list).

**3. Open a report** (`Reports` → click one)
- Edit field-verification details and set the **outcome / determination** (confirmed HAB, red
  tide, non-HAB algae, spill, other WQ, no bloom…).
- Add **field/lab results**, or **upload** a CEDEN lab CSV.
- Record a **response / advisory** (an advisory with "display on map" is what would drive the
  public map). See the **Locations & GeoConnex** section and per-report map.
- Staff-only: **reporter contact** and the **suspected illness/death** matrix.

**4. Explore the map** (`Map`)
- Reports plotted and colored by outcome. **Filters:** Region, County, Outcome, Advisory; date
  buttons (**15 / 30 / 60 / 120 days**); and **Analytical data** modes — *Events with lab data*
  or *Geocoded, not linked* (geocoded, unlinked, non-routine lab samples as teal markers, with a
  **Reconcile** link to the geocoded-unlinked workboard queue). Filters apply server-side;
  **Clear all** resets.

**5. Cases** (`Cases`)
- Group related reports into a case (one region, one waterbody, one year). Create a case, assign
  reports, set status/lead, and upload case-level lab data.

**6. Lab results browser** (`Lab`)
- Browse **all** field/lab results across reports. Filter by search (water body / analyte),
  analysis type, data type, region, sample-date range, and non-detects; sort; and **Download
  CSV** of the filtered set.

**7. Notifications** (`🔔`)
- Confirm you receive a notice when a new submission comes in (and an **⚠ illness alert** if one
  reports suspected illness). "Mark all read" clears the badge.

**8. Open data** (`Open data`)
- Download the published flat files (CSV or a zip), or view the **provisional JSON API**. Six
  datasets: the four FHAB files plus **CEDEN Chemistry Results** and a **crosswalk** (links each
  chemistry result to its watershed/GeoConnex and FHAB report/case).
- Confirm these contain **no** reporter contact / illness / veterinary data (they shouldn't).

**9. Admin only — Accounts, Groups, Analytes, Refresh & Reset**
- **Accounts:** create users and grant/revoke roles.
- **Groups:** register a community/partner group and mint an **API key** (shown once) so that
  group can submit attributed — and optionally "trusted" — reports.
- **Analytes:** curate the analyte vocabulary — edit name/class/unit, **merge** aliases
  (e.g. "mcyE" → Microcystins), and delete unused ones.
- **Refresh from data.ca.gov:** pull the latest published reports/cases/responses/results. Always
  **Preview** first (dry-run counts of *new / updated*); it inserts new and refreshes published
  fields, **preserves local-only data**, and **never deletes**. Type `UPDATE` to apply.
- **Reset / maintenance:** purge lab data to reset the test environment.

### A suggested 15-minute test script

1. Submit a test report from the CyanoSafe phone app (Track A) — include a suspected illness.
2. Log in to the Staff app; confirm the **🔔 bell** shows new + illness notifications.
3. Go to **Submissions**, **Promote** your test report to your region.
4. Open the new report; set an **outcome**, add a **field result**, and post an **advisory**.
5. Create a **Case** and assign the report to it.
6. Open the **Map**; filter by your region and the last 30 days, and find your report.
7. Open **Lab** and confirm your field result shows in the cross-report results browser.
8. Go to **Open data**; download **bloom-report.csv**; confirm your report is in it and that no
   reporter name/illness columns appear.
9. Note anything confusing along the way.

---

## Giving feedback

For each issue, please tell us:

- **Which app** (CyanoSafe phone / desktop / FHAB Staff) and **which page**.
- **What you did**, **what you expected**, and **what happened**.
- A **screenshot** if you can, and the **date/time** and your browser/device.
- Severity: blocker / annoying / cosmetic / suggestion.

Send feedback to **[contact provided separately]**. There's no in-app bug button yet.

---

## Known limitations (no need to report these)

These are known and on the roadmap — please don't file them as bugs:

- **No email yet** by default — notifications are in-app (the 🔔 bell). Email escalation is only
  active if the program configures SMTP.
- **No password reset / SSO** yet — if you're locked out, an admin resets your login.
- **No full audit history** yet (only a light "recent activity" log).
- **Publishing to data.ca.gov is manual** — the app generates the files; uploading them is a
  manual/scheduled step.
- **GeoConnex IDs** are shown but not yet publicly resolvable.
- The free hosting tier can be **slow on the first request** after idle (it "wakes up"); give it
  a few seconds.

---

## Mini glossary

- **Report** — one observed/suspected bloom event.
- **Event** — the first-class record a report becomes; some events become cases.
- **Case** — a grouping of related reports for a waterbody (one region, one year).
- **Response / Advisory** — a staff action; an advisory is the public-facing caution/warning/danger.
- **Determination / outcome** — what a report turned out to be.
- **Submission** — a public/community report awaiting staff review (not yet a tracked report).
- **Promote** — turn a pending submission into a tracked report.
- **Provisional data** — live data from this system, *not* the official data.ca.gov release.
- **CEDEN** — the state environmental data exchange (lab results format).
- **HUC-12 / GeoConnex** — watershed coding / persistent identifiers for locations.
