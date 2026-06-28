"""Public bloom-report submissions: a moderation queue fed by external apps (e.g. the
CyanoSafe phone demo), plus staff promotion of a submission into a real report.

The public endpoint (see web.public_submit) validates and inserts a *pending* submission; it
never creates a live event. Staff review the queue and either promote (-> enter_report as a
Public Reporting report, reusing the fuzzy waterbody dedup) or reject. This mirrors the real
program: a suspected report is triaged before it becomes a tracked event.
"""

from __future__ import annotations

import base64
from datetime import date, datetime

import psycopg

from .auth import acting_as
from .reports import enter_report

# Rough California bounding box for sanity-checking submitted coordinates.
CA_LAT = (32.3, 42.2)
CA_LON = (-124.6, -114.0)
MAX_PHOTO_BYTES = 5 * 1024 * 1024


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


def submit_public_report(conn: psycopg.Connection, payload: dict, *, source=None,
                         remote_ip=None) -> int:
    """Validate a public submission and insert it as 'pending'. Raises SubmissionError.

    Runs on the privileged connection (no logged-in user); the caller is responsible for
    abuse controls (rate limit, key, honeypot). Region/determination/status are NOT accepted
    from the public — those are decided by staff at promotion time.
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

    sid = conn.execute(
        """INSERT INTO public_report_submission
             (water_body_name, county, landmark, latitude, longitude, observation_date,
              bloom_size, bloom_location, bloom_textures, weather_condition,
              surface_water_condition, signs_posted, description, reporter_name,
              reporter_email, reporter_phone, reporter_org, photo, photo_content_type,
              source, remote_ip)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (name, county, _s(payload.get("landmark"), 200), lat, lon, obs,
         _s(payload.get("bloom_size"), 60), _s(payload.get("bloom_location"), 60), textures,
         _s(payload.get("weather_condition"), 40), _s(payload.get("surface_water_condition"), 40),
         _s(payload.get("signs_posted"), 40), _s(payload.get("description"), 2000),
         _s(payload.get("reporter_name"), 120), _s(payload.get("reporter_email"), 200),
         _s(payload.get("reporter_phone"), 40), _s(payload.get("reporter_org"), 160),
         photo, ctype, _s(source, 60), _s(remote_ip, 60))).fetchone()["id"]
    conn.commit()
    return sid


def list_submissions(conn: psycopg.Connection, user_id: int, status: str = "pending",
                     limit: int = 200) -> list:
    """List submissions for the staff review queue (under RLS)."""
    with acting_as(conn, user_id):
        return conn.execute(
            """SELECT id, submitted_at, status, water_body_name, county, landmark, latitude,
                      longitude, observation_date, reporter_name, source,
                      (photo IS NOT NULL) AS has_photo, promoted_report_id
               FROM public_report_submission
               WHERE (%s = 'all' OR status = %s)
               ORDER BY (status='pending') DESC, submitted_at DESC LIMIT %s""",
            (status, status, limit)).fetchall()


def reject_submission(conn: psycopg.Connection, user_id: int, sid: int, note=None) -> None:
    with acting_as(conn, user_id):
        conn.execute(
            """UPDATE public_report_submission SET status='rejected', review_note=%s,
                   reviewed_by=%s, reviewed_at=now() WHERE id=%s AND status='pending'""",
            (note, user_id, sid))
        conn.commit()


def promote_submission(conn: psycopg.Connection, user_id: int, sid: int, *, region=None) -> int:
    """Create a live Public Reporting report from a pending submission. Returns the report id."""
    sub = conn.execute(
        "SELECT * FROM public_report_submission WHERE id=%s AND status='pending'", (sid,)).fetchone()
    if not sub:
        raise SubmissionError("submission not found or already handled")
    brid = enter_report(
        conn, user_id, water_body_name=sub["water_body_name"] or "Unnamed waterbody",
        region=region, county=sub["county"], landmark=sub["landmark"],
        lat=sub["latitude"], lon=sub["longitude"], observation_date=sub["observation_date"],
        report_type="Public Reporting", bloom_size=sub["bloom_size"],
        bloom_location=sub["bloom_location"], bloom_textures=sub["bloom_textures"],
        surface_water_condition=sub["surface_water_condition"],
        weather_condition=sub["weather_condition"], signs_posted=sub["signs_posted"],
        has_pictures=bool(sub["photo"]), description=sub["description"],
        reporter_name=sub["reporter_name"], reporter_email=sub["reporter_email"],
        reporter_phone=sub["reporter_phone"], reporter_org=sub["reporter_org"],
        determination="under_investigation")
    with acting_as(conn, user_id):
        if sub["photo"]:
            conn.execute(
                """INSERT INTO report_photo (bloom_report_id, filename, content_type, data, uploaded_by)
                   VALUES (%s,%s,%s,%s,%s)""",
                (brid, "submission", sub["photo_content_type"], sub["photo"], user_id))
        conn.execute(
            """UPDATE public_report_submission SET status='promoted', promoted_report_id=%s,
                   reviewed_by=%s, reviewed_at=now() WHERE id=%s""", (brid, user_id, sid))
        conn.commit()
    return brid
