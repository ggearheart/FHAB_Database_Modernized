"""Public bloom-report submissions: a moderation queue fed by external apps (e.g. the
CyanoSafe phone demo), plus staff promotion of a submission into a real report.

The public endpoint (see web.public_submit) validates and inserts a *pending* submission; it
never creates a live event. Staff review the queue and either promote (-> enter_report as a
Public Reporting report, reusing the fuzzy waterbody dedup) or reject. This mirrors the real
program: a suspected report is triaged before it becomes a tracked event.
"""

from __future__ import annotations

import base64
import secrets
from datetime import date, datetime

import psycopg
from psycopg.types.json import Jsonb

from .auth import acting_as
from .reports import ILLNESS_SUBJECTS, enter_report, set_report_illness

# Rough California bounding box for sanity-checking submitted coordinates.
CA_LAT = (32.3, 42.2)
CA_LON = (-124.6, -114.0)
MAX_PHOTO_BYTES = 5 * 1024 * 1024

# A community/partner group's tier maps to the published report_type. (The open-data field has
# only Public vs Agency/Partner; finer attribution is kept in the group_name / source.)
TIER_REPORT_TYPE = {"public": "Public Reporting",
                    "community": "Agency/Partner Reporting",
                    "agency": "Agency/Partner Reporting"}


class SubmissionError(ValueError):
    """A public submission failed validation (message is safe to return to the caller)."""


def _s(v, n: int = 300):
    if v is None:
        return None
    v = str(v).strip()
    return v[:n] or None


def _coord(v, lo: float, hi: float):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def _parse_date(v):
    v = _s(v)
    if not v:
        return None
    try:
        return datetime.strptime(v[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _illness_from_payload(payload):
    """Validate the suspected illness/death matrix from a payload. Returns (rows, none_observed)."""
    rows = []
    for r in (payload.get("illness") or []):
        subj = _s(isinstance(r, dict) and r.get("subject"), 40)
        if subj in ILLNESS_SUBJECTS and (r.get("illness") or r.get("death")):
            rows.append({"subject": subj, "illness": bool(r.get("illness")),
                         "death": bool(r.get("death"))})
    return rows, bool(payload.get("no_illness_observed"))


def submit_public_report(conn: psycopg.Connection, payload: dict, *, source=None,
                         remote_ip=None, report_type=None, group_id=None, trusted=False) -> int:
    """Validate a public submission and insert it as 'pending'. Raises SubmissionError.

    Runs on the privileged connection (no logged-in user); the caller is responsible for
    abuse controls (rate limit, key, honeypot). region/determination/status are NOT accepted
    from the public, and report_type/group/trusted come ONLY from an authenticated group key
    (the caller), never from the payload — so a public submitter can't claim a partner tier.
    """
    name = _s(payload.get("water_body_name"), 200)
    if not name:
        raise SubmissionError("water_body_name is required")
    lat_in, lon_in = payload.get("latitude"), payload.get("longitude")
    lat = _coord(lat_in, *CA_LAT)
    lon = _coord(lon_in, *CA_LON)
    county = _s(payload.get("county"), 60)
    if lat is None and lon is None and not county:
        raise SubmissionError("a location (latitude/longitude or county) is required")
    if (lat_in not in (None, "") or lon_in not in (None, "")) and (lat is None or lon is None):
        raise SubmissionError("coordinates are missing or outside California")
    obs = _parse_date(payload.get("observation_date"))
    if obs and obs > date.today():
        raise SubmissionError("observation_date cannot be in the future")

    textures = payload.get("bloom_textures") or []
    if isinstance(textures, str):
        textures = [textures]
    textures = [_s(t, 40) for t in textures if _s(t, 40)][:15] or None

    photo = ctype = None
    raw = payload.get("photo_base64")
    if raw:
        try:
            photo = base64.b64decode(str(raw).split(",")[-1], validate=False)
        except Exception:  # noqa: BLE001
            raise SubmissionError("photo is not valid base64")
        if len(photo) > MAX_PHOTO_BYTES:
            raise SubmissionError("photo exceeds 5 MB")
        ctype = _s(payload.get("photo_content_type"), 80) or "image/jpeg"
        if not ctype.startswith("image/"):
            raise SubmissionError("photo must be an image")

    illness_rows, none_observed = _illness_from_payload(payload)

    sid = conn.execute(
        """INSERT INTO public_report_submission
             (water_body_name, county, landmark, latitude, longitude, observation_date,
              bloom_size, bloom_location, bloom_textures, weather_condition,
              surface_water_condition, signs_posted, description, reporter_name,
              reporter_email, reporter_phone, reporter_org, photo, photo_content_type,
              no_illness_observed, illness_description, illness,
              source, remote_ip, report_type, group_id, trusted)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (name, county, _s(payload.get("landmark"), 200), lat, lon, obs,
         _s(payload.get("bloom_size"), 60), _s(payload.get("bloom_location"), 60), textures,
         _s(payload.get("weather_condition"), 40), _s(payload.get("surface_water_condition"), 40),
         _s(payload.get("signs_posted"), 40), _s(payload.get("description"), 2000),
         _s(payload.get("reporter_name"), 120), _s(payload.get("reporter_email"), 200),
         _s(payload.get("reporter_phone"), 40), _s(payload.get("reporter_org"), 160),
         photo, ctype, none_observed, _s(payload.get("illness_description"), 2000),
         Jsonb(illness_rows) if illness_rows else None,
         _s(source, 60), _s(remote_ip, 60), _s(report_type, 40), group_id,
         bool(trusted))).fetchone()["id"]
    conn.commit()
    return sid


def list_submissions(conn: psycopg.Connection, user_id: int, status: str = "pending",
                     limit: int = 200, trusted_only: bool = False) -> list:
    """List submissions for the staff review queue (under RLS)."""
    with acting_as(conn, user_id):
        return conn.execute(
            """SELECT id, submitted_at, status, water_body_name, county, landmark, latitude,
                      longitude, observation_date, reporter_name, source, report_type, trusted,
                      (illness IS NOT NULL) AS has_illness,
                      (photo IS NOT NULL) AS has_photo, promoted_report_id
               FROM public_report_submission
               WHERE (%s = 'all' OR status = %s) AND (NOT %s OR trusted)
               ORDER BY (status='pending') DESC, trusted DESC, submitted_at DESC LIMIT %s""",
            (status, status, trusted_only, limit)).fetchall()


def reject_submission(conn: psycopg.Connection, user_id: int, sid: int, note=None) -> None:
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE public_report_submission SET status='rejected', review_note=%s,
                   reviewed_by=%s, reviewed_at=now() WHERE id=%s AND status='pending'""",
            (note, user_id, sid))
        conn.commit()


def _promote_one(conn, user_id, sub, region=None) -> int:
    """Materialize one pending submission into a live report (carrying its full-form fields)."""
    brid = enter_report(
        conn, user_id, water_body_name=sub["water_body_name"] or "Unnamed waterbody",
        region=region, county=sub["county"], landmark=sub["landmark"],
        lat=sub["latitude"], lon=sub["longitude"], observation_date=sub["observation_date"],
        report_type=sub["report_type"] or "Public Reporting", bloom_size=sub["bloom_size"],
        bloom_location=sub["bloom_location"], bloom_textures=sub["bloom_textures"],
        surface_water_condition=sub["surface_water_condition"],
        weather_condition=sub["weather_condition"], signs_posted=sub["signs_posted"],
        has_pictures=bool(sub["photo"]), description=sub["description"],
        reporter_name=sub["reporter_name"], reporter_email=sub["reporter_email"],
        reporter_phone=sub["reporter_phone"], reporter_org=sub["reporter_org"],
        determination="under_investigation")
    if sub["illness"] or sub["no_illness_observed"] or sub["illness_description"]:
        set_report_illness(conn, user_id, brid, rows=sub["illness"] or [],
                           none_observed=bool(sub["no_illness_observed"]),
                           description=sub["illness_description"])
    with acting_as(conn, user_id):
        if sub["photo"]:
            conn.execute(
                """INSERT INTO report_photo (bloom_report_id, filename, content_type, data, uploaded_by)
                   VALUES (%s,%s,%s,%s,%s)""",
                (brid, "submission", sub["photo_content_type"], sub["photo"], user_id))
        conn.execute(
            """UPDATE public_report_submission SET status='promoted', promoted_report_id=%s,
                   reviewed_by=%s, reviewed_at=now() WHERE id=%s""", (brid, user_id, sub["id"]))
        conn.commit()
    return brid


def promote_submission(conn: psycopg.Connection, user_id: int, sid: int, *, region=None) -> int:
    """Create a live report from a pending submission. Returns the report id."""
    sub = conn.execute(
        "SELECT * FROM public_report_submission WHERE id=%s AND status='pending'", (sid,)).fetchone()
    if not sub:
        raise SubmissionError("submission not found or already handled")
    return _promote_one(conn, user_id, sub, region)


def promote_trusted_pending(conn: psycopg.Connection, user_id: int) -> int:
    """Promote every pending submission from a trusted group (the lighter-touch lane)."""
    subs = conn.execute(
        "SELECT * FROM public_report_submission WHERE status='pending' AND trusted ORDER BY id"
    ).fetchall()
    for sub in subs:
        _promote_one(conn, user_id, sub)
    return len(subs)


# ---------- Community/partner group registry (API keys) ----------

def resolve_intake_group(conn: psycopg.Connection, api_key: str):
    """Look up an active group by API key (privileged read; the key is the credential)."""
    if not api_key:
        return None
    return conn.execute(
        "SELECT id, group_name, tier, trusted FROM intake_group WHERE api_key=%s AND active",
        (api_key,)).fetchone()


def create_intake_group(conn: psycopg.Connection, user_id: int, group_name: str, *,
                        tier: str = "community", trusted: bool = False):
    """Register a group and mint its API key (returned once). Admin-only via RLS."""
    if tier not in TIER_REPORT_TYPE:
        raise SubmissionError("tier must be public, community, or agency")
    api_key = "fhabg_" + secrets.token_urlsafe(24)
    with acting_as(conn, user_id):
        gid = conn.execute(
            """INSERT INTO intake_group (group_name, tier, api_key, trusted, created_by)
               VALUES (%s,%s,%s,%s,%s) RETURNING id""",
            (group_name.strip(), tier, api_key, bool(trusted), user_id)).fetchone()["id"]
        conn.commit()
    return gid, api_key


def list_intake_groups(conn: psycopg.Connection, user_id: int) -> list:
    with acting_as(conn, user_id):
        return conn.execute(
            """SELECT id, group_name, tier, trusted, active, created_at,
                      left(api_key, 12) || '…' AS key_prefix
               FROM intake_group ORDER BY active DESC, group_name""").fetchall()


def set_group_active(conn: psycopg.Connection, user_id: int, gid: int, active: bool) -> None:
    with acting_as(conn, user_id):
        conn.execute("UPDATE intake_group SET active=%s WHERE id=%s", (bool(active), gid))
        conn.commit()
