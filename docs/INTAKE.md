# Public bloom-report intake API

External apps and partner groups submit suspected blooms to one endpoint. Submissions land in
a **staff moderation queue** — nothing appears on the map until a staffer promotes it. This
mirrors the program's real flow: a suspected report is triaged before it becomes a tracked event.

- **Endpoint:** `POST /api/public/reports`
- **Production base URL:** `https://fhab-web.onrender.com`
- **Content-Type:** `application/json`
- **Auth:** none for anonymous public; an optional API key identifies a registered group (below).

## Quick start

```bash
curl -X POST https://fhab-web.onrender.com/api/public/reports \
  -H "Content-Type: application/json" \
  -d '{
    "water_body_name": "Clear Lake",
    "county": "Lake",
    "latitude": 39.05, "longitude": -122.82,
    "observation_date": "2026-06-26",
    "bloom_size": "larger than a football field",
    "bloom_textures": ["Surface scum"],
    "description": "Bright green scum along the north shore",
    "reporter_name": "Jane Q", "reporter_email": "jane@example.com",
    "source": "my-app"
  }'
```

Success → `200`:
```json
{ "ok": true, "id": 123, "message": "Thank you — your report was received for review." }
```
Failure → `400` (validation), `401` (bad key), `429` (rate limited):
```json
{ "ok": false, "error": "water_body_name is required" }
```

## Fields

Required: **`water_body_name`**, and **a location** — either `latitude`+`longitude` *or* `county`.

| Field | Type | Notes |
|---|---|---|
| `water_body_name` | string | **required**, ≤200 |
| `county` | string | ≤60; required if no lat/lon |
| `landmark` | string | ≤200 |
| `latitude`, `longitude` | number | must be inside California; send **both** or neither |
| `observation_date` | string | `YYYY-MM-DD`; not in the future |
| `bloom_size` | string | see vocab |
| `bloom_location` | string | distance from shore; see vocab |
| `bloom_textures` | string[] | multi-select; see vocab; ≤15 |
| `weather_condition` | string | see vocab |
| `surface_water_condition` | string | see vocab |
| `signs_posted` | string | see vocab |
| `description` | string | ≤2000 |
| `reporter_name` / `reporter_email` / `reporter_phone` / `reporter_org` | string | reporter contact (kept internal — never published) |
| `no_illness_observed` | boolean | |
| `illness` | array | suspected illness/death matrix (below) |
| `illness_description` | string | ≤2000 |
| `photo_base64` | string | data-URL or raw base64; ≤5 MB decoded |
| `photo_content_type` | string | e.g. `image/jpeg` (defaults if omitted) |
| `source` | string | free-text app id (anonymous only); ≤60 |
| `website` | string | **honeypot — leave empty** (a value silently discards the submission) |

**Set by the server, ignored if sent in the body:** `report_type`, `group_id`, `trusted`,
`region`, `determination`, `status`. (Anonymous submissions are always Public Reporting and
untrusted; a partner tier comes only from an authenticated key — see below.)

### Suspected illness / death

```json
"illness": [
  { "subject": "Dog", "illness": true, "death": false },
  { "subject": "Human", "illness": true, "death": false }
],
"no_illness_observed": false,
"illness_description": "A dog was sick after wading."
```
Valid `subject` values: **Human, Dog, Pet, Fish, Wildlife, Cattle, Goat, Horse, Sheep,
Livestock**. Rows with neither `illness` nor `death` are dropped. This data is sensitive: it is
stored internal-only and is **never** included in the public map or open-data export.

### Photo

Send a base64 string (a `data:image/...;base64,...` data-URL is fine — the server takes the part
after the comma). Max 5 MB decoded; `content_type` must be an image.

## Controlled vocabularies

Free text is accepted, but using these values keeps the data clean and matches the official form:

- **bloom_size:** `larger than a football field` · `between a football field and a tennis court` · `between a tennis court and a sedan` · `smaller than a sedan` · `no bloom`
- **bloom_location:** `<10 feet from shore` · `10-50 feet from shore` · `>50 feet from shore` · `shoreline to >50 feet from shore` · `no bloom`
- **bloom_textures:** `Streaking` · `Surface scum` · `Floating mats` · `Stranded mats` · `Benthic mats` · `Spilled paint` · `Green discoloration` · `Visible spherical colonies` · `Grass clippings` · `Other` · `No bloom`
- **weather_condition:** `Clear` · `Partly cloudy` · `Overcast` · `Rain`
- **surface_water_condition:** `Calm` · `Ripples` · `Choppy` · `White caps`
- **signs_posted:** `None` · `General awareness` · `Caution` · `Warning` · `Danger`

## Community / partner groups (API keys)

Registered groups submit with a key for **attribution** and an optional **trust lane**. Program
admins mint keys in the staff app under **Groups** (`/admin/intake-groups`); the key is shown
once.

Send it as a header:
```bash
curl -X POST https://fhab-web.onrender.com/api/public/reports \
  -H "Content-Type: application/json" \
  -H "X-API-Key: fhabg_xxxxxxxxxxxxxxxxxxxx" \
  -d '{ "water_body_name": "Clear Lake", "county": "Lake", "observation_date": "2026-06-26" }'
```

A keyed submission is:
- **attributed** to the group (the group name becomes the `source`), and
- **tiered** — `community` / `agency` groups are recorded as `Agency/Partner Reporting`; and
- if the group is marked **trusted**, flagged for the lighter-touch review lane (staff can filter
  to trusted and bulk-promote them).

> The tier and trust come from the key, not the request — a body that claims a partner tier
> without a valid key is ignored and treated as anonymous public. Trusted still means *reviewed*
> (a staffer bulk-promotes); it does not auto-publish. Revoked keys stop working immediately.

**Note on key secrecy:** a key embedded in a public web/mobile client is not truly secret — treat
it as attribution + a throttle handle, not authentication. The real protections are the
moderation queue, the per-IP rate limit, and the honeypot. For genuinely trusted automation,
keep the key server-side.

## CORS, rate limit, moderation

- **CORS:** browser clients must call from an allowlisted origin (`PUBLIC_INTAKE_ORIGINS`,
  e.g. `https://ggearheart.github.io`). Server-to-server calls (curl, backends) ignore CORS.
- **Rate limit:** ~10 submissions/hour per IP (default) → `429` when exceeded.
- **Moderation:** every submission is `pending`. Staff **promote** it (→ a real report, carrying
  all fields incl. illness and photo, with fuzzy waterbody dedup) or **reject** it. Nothing
  reaches the public map or open data until promoted.

## JavaScript example

```js
async function reportBloom(data, apiKey) {
  const headers = { 'Content-Type': 'application/json' };
  if (apiKey) headers['X-API-Key'] = apiKey;            // partner groups only
  const r = await fetch('https://fhab-web.onrender.com/api/public/reports',
    { method: 'POST', headers, body: JSON.stringify({ ...data, source: 'my-app' }) });
  const j = await r.json();
  if (!(r.ok && j.ok)) throw new Error(j.error || 'submission failed');
  return j.id;
}
```
