# User Roles & Permissions

**Status: design for interface development.** Defines who uses the modernized FHAB system
and what each can do, so the UI and the database's access control can be built against a
shared model. It covers the **legacy roles** (carried forward — grounded in the
case-management *User Manual* and the old `LookUp_Personnel` structure) **plus** the
**Internet of Water-envisioned contributor roles** and a new **Water Body Manager** role.

> **What the manual showed.** The legacy FHAB web app had essentially **one authenticated
> user type — FHAB / Water Board staff** (region-scoped via a region filter), who did the
> whole workflow: review reports → assign to **cases** → record **responses**/advisories →
> enter **field and lab data** → publish to the web map. The one distinct specialization was
> the **Interagency HAB-related Illness Workgroup** (enters tissue results, investigates
> suspected illness). Everyone else — responding organizations, water/land managers, DDW,
> local public health, the public — were **report submitters and notification recipients**,
> *not* logged-in users. The modernization's job is to turn those external parties into
> first-class roles (contributors, managers) while keeping the staff workflow intact.

## Role catalog

Roles fall into four categories. Each role also carries a **scope** (see below) — the same
role means different rows for different people.

### A. Internal State staff (legacy — carried forward)

| Role | What they do |
|------|--------------|
| **Program Administrator (OIMA)** | Owns the FHAB Program: manages the data model, controlled vocabularies, users/roles, and public publishing. Statewide. |
| **Water Board Staff (Regional)** | The core role. Triage reports, open/manage **cases**, conduct **responses**, recommend/post **advisories**, verify blooms, **and enter field & lab data**. Scoped to their **Regional Board** (region filter). A *Case Lead* is a Water Board Staff member assigned to a case. |
| **Illness Workgroup Staff** | The Interagency HAB-related Illness Workgroup. Enters tissue/illness **lab results** and investigates **suspected illness** reports. Sees sensitive illness/veterinary data that is withheld from other roles. |
| **Data Viewer (internal)** | Read-only internal access across the lifecycle. |

> In the legacy system, **field-data entry, lab-data entry, and DDW review were *functions*
> performed by Water Board staff (or notification recipients), not separate login roles.**
> The modern system *may* split out **Field Staff**, **Lab Analyst**, and **DDW Staff** as
> distinct roles (scoped to data type / DDW district) if the program wants finer separation
> of duties — listed here as optional refinements rather than legacy requirements.

### B. External contributors (IoW-envisioned — new)

| Role | What they do |
|------|--------------|
| **Tribal Government Admin** | Submit & manage their organization's monitoring (stations, samples, results, Tier 1–3 posts); manage their contributors; **retain data ownership** and control sharing. Org-scoped. |
| **Community Science Program Manager** | Manage a volunteer monitoring program: curate contributors, run QAQC, submit/forward data. Org-scoped. |
| **Community Science Volunteer** | Submit Tier 1 posts / Tier 2 qualitative observations (and Tier 3 readings at assigned sites). Self-scoped. |
| **Partner Agency** | State/local partners (e.g., DWR, regional parks) that share monitoring data. Org-scoped. |

### C. Managers (new)

| Role | What they do |
|------|--------------|
| **Water Body Manager** | Manages a specific waterbody / surrounding land (legacy `WaterBodyManager` / `RecLandManager` / `ManagementOrgs`). Sees events, advisories, and results **for their waterbody(ies)**; receives alerts; can add local context/notes and confirm signage posted. Waterbody-scoped. |

### D. Public

| Role | What they do |
|------|--------------|
| **Public / Consumer** | Read **published** advisories and the public map only (`display_advisory_on_map = true`). |
| **Decision-Maker / Public Health Official** | Elevated read for situational awareness + alert subscriptions. |

## Permissions matrix

`C`reate · `R`ead · `U`pdate · `V`erify/QAQC · `P`ublish (to public map) · `X` export.
Read is scoped (see below); blank = no access.

| Role \ Entity | Report/Event | Case | Response/Advisory | Result/Sample | Station | Contributor data (Tier 1–3) | Lookups | Users |
|---|---|---|---|---|---|---|---|---|
| Program Administrator | CRUVP X | CRUVP | CRUVP | CRUV X | CRU | R V | CRUD | CRUD |
| Water Board Staff (Regional) | CRUV | CRUV | CRUVP | CRUV | CRU | R V | R | — |
| Illness Workgroup Staff | R | R | R | CRU (tissue/illness) | R | — | R | — |
| Data Viewer (internal) | R | R | R | R | R | R | R | — |
| *Field Staff (optional)* | R | R | R | CRU (field) | CR | — | R | — |
| *Lab Analyst (optional)* | R | R | R | CRU (lab) | R | — | R | — |
| *DDW Staff (optional)* | R | R | R (advise) | R | R | — | R | — |
| Tribal Government Admin | C R (own) | — | — | CRU (own) | CRU (own) | CRUV (own) | R | — (own contributors) |
| Comm. Sci. Program Manager | C R (own) | — | — | CRU (own) | CRU (own) | CRUV (own) | R | — (own contributors) |
| Comm. Sci. Volunteer | C (post) | — | — | C (own) | — | C R (own) | R | — |
| Partner Agency | C R (own) | — | — | CRU (own) | CRU (own) | CRU (own) | R | — |
| Water Body Manager | R (own WB) | R (own WB) | R (own WB) | R (own WB) | R (own WB) | R (own WB) | R | — |
| Decision-Maker / PHO | R (published+) | R | R (published) | — | — | — | — | — |
| Public / Consumer | R (published) | — | R (published) | — | — | — | — | — |

Key rules baked in:
- **Verify/Publish is staff-only.** Contributors (B) submit data carrying a **review
  status** (`MGT-2`); only Program Admin / Water Board Staff can verify it and set
  `display_advisory_on_map`. Nothing reaches the public map unverified.
- **Contributors retain ownership** (IoW principle). A contributor org can edit only *its
  own* data; sharing into the State workflow is explicit, and State staff get read (and
  verify) but the org keeps authorship.
- **Field vs Lab results** are write-partitioned by `data_type` so field crews and labs
  don't overwrite each other.

## Scoping model

A role assignment is *role + scope*. The scope dimensions mirror the legacy
`Personnel`/lookup structure and the new contributor model:

| Scope | Applies to | Source |
|-------|-----------|--------|
| **Region** | Water Board Staff | `regional_water_board` |
| **DDW district** | DDW Staff | `ddw_district` |
| **Organization** | Tribal / Community / Partner roles | `organization` / `management_org` |
| **Waterbody** | Water Body Manager | `waterbody` (one or many) |
| **Data tier** | Contributor visibility | Tier 1/2/3 |
| **Statewide** | Program Administrator | — |

This is naturally enforced with **PostgreSQL Row-Level Security**: policies filter rows by
the current user's role + scope (set per session), so the same query returns each user only
their permitted rows, and the public sees only published advisories.

## Proposed schema (sketch)

Builds on the `personnel` table from the legacy review.

```sql
CREATE TABLE app_user (
    id             bigserial PRIMARY KEY,
    personnel_code text REFERENCES personnel(personnel_code),  -- internal staff
    email          text UNIQUE NOT NULL,
    full_name      text,
    is_active      boolean NOT NULL DEFAULT true
);

CREATE TABLE role (
    code        text PRIMARY KEY,        -- 'program_admin', 'wb_staff', 'tribal_admin', …
    name        text NOT NULL,
    category    text NOT NULL,           -- internal_staff | contributor | manager | public
    description text
);

-- A user holds a role within a scope (NULLs = unscoped / statewide).
CREATE TABLE user_role (
    user_id        bigint REFERENCES app_user(id),
    role_code      text   REFERENCES role(code),
    scope_region   text,
    scope_ddw_district text,
    scope_org      text,           -- organization / management_org
    scope_waterbody_id bigint REFERENCES waterbody(id),
    granted_by     bigint REFERENCES app_user(id),
    granted_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role_code, scope_region, scope_ddw_district, scope_org, scope_waterbody_id)
);
```

RLS policies then key off `current_setting('fhab.user_id')` to resolve the user's roles +
scopes at query time.

## Resolved by the manual

- **Legacy roles** — one authenticated staff role (region-scoped) + the Illness Workgroup;
  "Case Lead" is an *assignment*, not a role. ✅
- **PII / private data** — fields labeled "… notes" and reporter/illness/veterinary data are
  **private and not published** ("only bold+underline fields are displayed to the public").
  Withholding from non-staff roles is therefore a real requirement, not an assumption. ✅
- **Publishing** — staff set "display report to map"; verification/publishing is a
  staff function. ✅

## Open items

1. **Optional internal splits** — does the program want **Field Staff / Lab Analyst / DDW
   Staff** as distinct login roles, or keep them as functions of Water Board Staff (legacy
   behavior)?
2. **Water Body Manager actions** — read-only + notes/signage-confirmation (assumed), or
   able to submit observations like a contributor?
3. **Contributor → case workflow** — when a Tribal/community submission arrives, does it
   auto-create an unassigned report for staff triage (matching the legacy "Unassigned Bloom
   Reports" queue), or land in a separate contributor area first?
