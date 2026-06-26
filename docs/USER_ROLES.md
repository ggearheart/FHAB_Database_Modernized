# User Roles & Permissions

**Status: design for interface development.** Defines who uses the modernized FHAB system
and what each can do, so the UI and the database's access control can be built against a
shared model. It covers the **legacy roles** (carried forward — inferred from the old
system's `LookUp_Personnel.PrimaryRole`, agency/region/DDW scoping, and the data dictionary;
to be confirmed against the training manual) **plus** the **Internet of Water-envisioned
contributor roles** and a new **Water Body Manager** role.

## Role catalog

Roles fall into four categories. Each role also carries a **scope** (see below) — the same
role means different rows for different people.

### A. Internal State staff (legacy — carried forward)

| Role | What they do |
|------|--------------|
| **Program Administrator (OIMA)** | Owns the FHAB Program: manages the data model, controlled vocabularies, users/roles, and public publishing. Statewide. |
| **Water Board Staff (Regional)** | Triage reports, open/manage **cases**, conduct **responses**, recommend/post **advisories**, verify blooms. Scoped to their **Regional Board**. (A *Case Lead* is a Water Board Staff member assigned to a case.) |
| **Field Staff** | Record field visits, field-visual and field-measurement **results**; collect **samples**. |
| **Lab Analyst** | Enter/curate laboratory **results** (microscopy, genetic, cyanotoxin) against samples. |
| **DDW Staff** | Division of Drinking Water review for drinking-water waterbodies; scoped to a **DDW district**. |
| **Data Viewer (internal)** | Read-only internal access across the lifecycle. |

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
| Field Staff | R | R | R | CRU (field) | CR | — | R | — |
| Lab Analyst | R | R | R | CRU (lab) | R | — | R | — |
| DDW Staff | R | R | R (advise) | R | R | — | R | — |
| Data Viewer (internal) | R | R | R | R | R | R | R | — |
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

## Open items

1. **Confirm the exact legacy roles** against the training manual — names, and whether
   "Case Lead", "QA Reviewer", or others were distinct roles vs. assignments. The list in
   section A is inferred from the data and dictionary.
2. **Verification authority** — is advisory *posting* (to the public map) limited to a
   specific role/seniority beyond "Water Board Staff"?
3. **Water Body Manager actions** — read-only + notes/signage-confirmation (assumed), or
   should they be able to submit observations like a contributor?
4. **Public PII** — confirm reporter contact info and veterinary/illness data are withheld
   from non-staff roles (assumed yes).
