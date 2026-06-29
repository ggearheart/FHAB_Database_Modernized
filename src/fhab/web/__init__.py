"""Staff web app: role-based account management and report data entry (Flask).

Data operations run through the database's Row-Level Security as the logged-in user
(`fhab.auth.acting_as`); account management runs with the privileged connection but is gated
to `program_admin` at the app layer. See docs/USER_ROLES.md.
"""

from __future__ import annotations

import os
from functools import wraps

import psycopg
from flask import (Flask, flash, g, jsonify, redirect, render_template, request, session, url_for)

import tempfile

from ..auth import (acting_as, authenticate, create_user, grant_role, list_roles_for,
                    revoke_role, set_password, user_regions)
from ..cases import (CASE_STATUSES, assign_report_to_case, create_case, update_case)
from ..ceden import (load_ceden_output, load_chemistry_for_case, load_chemistry_for_event)
from ..labmatch import (_candidates, auto_match, create_event_from_stage, link_stage_sample,
                        skip_stage_sample, stage_batch)
from ..places import COUNTIES, similar_waterbodies, suggest_waterbodies
from ..intake import (SubmissionError, create_intake_group, list_intake_groups, list_submissions,
                      promote_submission, promote_trusted_pending, reject_submission,
                      resolve_intake_group, set_group_active, submit_public_report)
from ..export import DATASETS, fetch_flatfile
from ..labquery import count_results, filter_options, query_results
from ..notify import (list_notifications, mark_read, on_new_submission, unread_count)
from ..db import DEFAULT_DSN, connect

# Simple in-memory per-IP rate limiter for the public submission endpoint. Process-local (resets
# on restart, not shared across workers) — adequate for the demo; use a shared store in prod.
import time as _time
_RATE: dict[str, list] = {}


def _rate_ok(ip: str, limit: int = 10, window: int = 3600) -> bool:
    now = _time.time()
    c = _RATE.get(ip)
    if not c or now - c[1] > window:
        _RATE[ip] = [1, now]
        return True
    if c[0] >= limit:
        return False
    c[0] += 1
    return True


# Short-TTL cache for the public open-data JSON endpoints, to shield the DB from repeated hits.
_OPEN_CACHE: dict[str, tuple] = {}
_OPEN_TTL = 600  # seconds


def _jsonable(v):
    from datetime import date, datetime
    from decimal import Decimal
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _csv_text(headers, records) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for rec in records:
        w.writerow(["" if rec[h] is None else rec[h] for h in headers])
    return buf.getvalue()
from ..geo import GEOCONNEX
from ..reports import (ILLNESS_SUBJECTS, add_response, add_result, enter_report,
                       set_report_illness, update_report)


def case_locations(conn, case_id):
    """All location sources across a case's reports, plus case-level CEDEN stations."""
    out = []
    for r in conn.execute(
            "SELECT bloom_report_id FROM event WHERE case_id = %s ORDER BY bloom_report_id",
            (case_id,)).fetchall():
        for loc in report_locations(conn, r["bloom_report_id"]):
            loc = dict(loc)
            loc["label"] = f"R{r['bloom_report_id']}: {loc['label']}"
            out.append(loc)
    for st in conn.execute(
            """SELECT DISTINCT st.id, st.station_code, st.station_name, ST_Y(st.geom) AS lat,
                      ST_X(st.geom) AS lon, st.huc12, st.geoconnex_uri
               FROM sample s JOIN station st ON st.id = s.station_id
               WHERE s.case_id = %s AND s.bloom_report_id IS NULL AND st.geom IS NOT NULL""",
            (case_id,)).fetchall():
        label = (st["station_code"] or "")
        if st["station_name"]:
            label = f"{label} — {st['station_name']}".strip(" —")
        out.append({"kind": "CEDEN station (case)", "label": label or "Station",
                    "lat": st["lat"], "lon": st["lon"], "huc12": (st["huc12"] or "").strip() or None,
                    "pid": st["geoconnex_uri"] or f"{GEOCONNEX}/sites/{st['id']}",
                    "minted": bool(st["geoconnex_uri"]), "color": "#2e8b57"})
    return out


def report_locations(conn, brid):
    """Every location source for a report: reporting point, CEDEN station(s), sample point(s),
    each with its HUC-12 watershed and GeoConnex PID (proposed if not yet minted)."""
    out = []
    rep = conn.execute(
        """SELECT ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon, l.huc12, l.landmark, e.geoconnex_uri
           FROM event e JOIN location l ON l.id = e.location_id
           WHERE e.bloom_report_id = %s AND l.geom IS NOT NULL""", (brid,)).fetchone()
    if rep:
        out.append({"kind": "Reporting location", "label": rep["landmark"] or "Report point",
                    "lat": rep["lat"], "lon": rep["lon"], "huc12": (rep["huc12"] or "").strip() or None,
                    "pid": rep["geoconnex_uri"] or f"{GEOCONNEX}/events/{brid}",
                    "minted": bool(rep["geoconnex_uri"]), "color": "#2563eb"})
    for st in conn.execute(
        """SELECT DISTINCT st.id, st.station_code, st.station_name, ST_Y(st.geom) AS lat,
                  ST_X(st.geom) AS lon, st.huc12, st.geoconnex_uri
           FROM sample s JOIN station st ON st.id = s.station_id
           WHERE s.bloom_report_id = %s AND st.geom IS NOT NULL
           ORDER BY st.station_code""", (brid,)).fetchall():
        label = (st["station_code"] or "")
        if st["station_name"]:
            label = f"{label} — {st['station_name']}".strip(" —")
        out.append({"kind": "CEDEN station", "label": label or "Station",
                    "lat": st["lat"], "lon": st["lon"], "huc12": (st["huc12"] or "").strip() or None,
                    "pid": st["geoconnex_uri"] or f"{GEOCONNEX}/sites/{st['id']}",
                    "minted": bool(st["geoconnex_uri"]), "color": "#2e8b57"})
    for sl in conn.execute(
        """SELECT DISTINCT l.id, ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon, l.huc12
           FROM sample s JOIN location l ON l.id = s.location_id
           WHERE s.bloom_report_id = %s AND l.geom IS NOT NULL""", (brid,)).fetchall():
        out.append({"kind": "Sample location", "label": "Sample point",
                    "lat": sl["lat"], "lon": sl["lon"], "huc12": (sl["huc12"] or "").strip() or None,
                    "pid": None, "minted": False, "color": "#b35900"})
    return out


def create_app(dsn: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = (
        os.environ.get("SECRET_KEY") or os.environ.get("FHAB_SECRET") or "dev-secret-change-me"
    )
    app.config["DSN"] = dsn or DEFAULT_DSN

    # ---- connection per request ----
    def db():
        if "conn" not in g:
            g.conn = connect(app.config["DSN"])
        return g.conn

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("conn", None)
        if c is not None:
            c.close()

    # ---- auth guards ----
    def login_required(f):
        @wraps(f)
        def w(*a, **k):
            if "uid" not in session:
                return redirect(url_for("login", next=request.path))
            return f(*a, **k)
        return w

    def admin_required(f):
        @wraps(f)
        @login_required
        def w(*a, **k):
            if "program_admin" not in session.get("roles", []):
                flash("Administrator access required.", "error")
                return redirect(url_for("dashboard"))
            return f(*a, **k)
        return w

    STAFF_WRITER_ROLES = {"program_admin", "wb_staff", "field_staff", "lab_analyst",
                          "illness_workgroup", "ddw_staff"}

    def staff_required(f):
        @wraps(f)
        @login_required
        def w(*a, **k):
            if not STAFF_WRITER_ROLES & set(session.get("roles", [])):
                flash("Staff access required.", "error")
                return redirect(url_for("dashboard"))
            return f(*a, **k)
        return w

    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    def _yn(v):
        """Map a Yes/No radio to a tri-state bool (None when unanswered)."""
        return {"Yes": True, "No": False}.get((v or "").strip())

    def _illness_rows(form):
        """Parse the illness/death matrix checkboxes (illness_<Subject>, death_<Subject>)."""
        return [{"subject": s, "illness": bool(form.get(f"illness_{s}")),
                 "death": bool(form.get(f"death_{s}"))} for s in ILLNESS_SUBJECTS]

    def _record_activity(brid, action):
        """Best-effort log that the current user worked on a report (never breaks the request)."""
        try:
            db().execute(
                "INSERT INTO report_activity (user_id, bloom_report_id, action) VALUES (%s,%s,%s)",
                (session.get("uid"), brid, action))
            db().commit()
        except Exception:  # noqa: BLE001
            try:
                db().rollback()
            except Exception:  # noqa: BLE001
                pass

    def _regions():
        return [r["regional_water_board"] for r in db().execute(
            "SELECT DISTINCT regional_water_board FROM waterbody "
            "WHERE regional_water_board IS NOT NULL ORDER BY 1").fetchall()]

    def _determinations():
        return db().execute(
            "SELECT code, label FROM report_determination ORDER BY sort_order").fetchall()

    def _analytes():
        return db().execute(
            "SELECT id, analysis_type, analyte, default_unit FROM analyte "
            "WHERE analyte IS NOT NULL ORDER BY analysis_type, analyte").fetchall()

    def _recommended_advisories():
        return db().execute(
            "SELECT code, label FROM recommended_advisory ORDER BY sort_order").fetchall()

    DATA_TYPES = ["Field Visual", "Field Measurement", "Laboratory"]
    RESPONSE_CATEGORIES = ["Advisory", "Investigation", "Field response", "Notification"]

    # Controlled vocabularies from the official MyWaterQuality bloom-report form.
    REPORT_TYPES = ["Public Reporting", "Agency/Partner Reporting"]
    SIGNS_OPTIONS = ["None", "General awareness", "Caution", "Warning", "Danger"]
    WEATHER_OPTIONS = ["Clear", "Partly cloudy", "Overcast", "Rain"]
    SURFACE_WATER_OPTIONS = ["Calm", "Ripples", "Choppy", "White caps"]
    SIZE_OPTIONS = ["larger than a football field",
                    "between a football field and a tennis court",
                    "between a tennis court and a sedan", "smaller than a sedan", "no bloom"]
    BLOOM_LOCATION_OPTIONS = ["<10 feet from shore", "10-50 feet from shore",
                              ">50 feet from shore", "shoreline to >50 feet from shore", "no bloom"]
    TEXTURE_OPTIONS = ["Streaking", "Surface scum", "Floating mats", "Stranded mats",
                       "Benthic mats", "Spilled paint", "Green discoloration",
                       "Visible spherical colonies", "Grass clippings", "Other", "No bloom"]
    # Public submission endpoint config (CORS allowlist + optional shared key).
    PUBLIC_ORIGINS = [o.strip() for o in os.environ.get(
        "PUBLIC_INTAKE_ORIGINS", "https://ggearheart.github.io").split(",") if o.strip()]
    PUBLIC_KEY = os.environ.get("PUBLIC_INTAKE_KEY")

    VOCAB = dict(report_types=REPORT_TYPES, signs_options=SIGNS_OPTIONS,
                 weather_options=WEATHER_OPTIONS, surface_water_options=SURFACE_WATER_OPTIONS,
                 size_options=SIZE_OPTIONS, bloom_location_options=BLOOM_LOCATION_OPTIONS,
                 texture_options=TEXTURE_OPTIONS, illness_subjects=ILLNESS_SUBJECTS,
                 counties=COUNTIES)

    @app.context_processor
    def _inject_nav():
        """Expose the logged-in user's unread-notification count to every template."""
        if not session.get("uid"):
            return {"nav_unread": 0}
        try:
            return {"nav_unread": unread_count(db(), session["uid"])}
        except Exception:  # noqa: BLE001
            return {"nav_unread": 0}

    # ---- routes ----
    @app.route("/notifications")
    @login_required
    def notifications():
        return render_template("notifications.html",
                               items=list_notifications(db(), session["uid"]))

    @app.route("/notifications/read-all", methods=["POST"])
    @login_required
    def notifications_read_all():
        mark_read(db(), session["uid"])
        return redirect(url_for("notifications"))

    @app.route("/notifications/<int:nid>/open")
    @login_required
    def notification_open(nid):
        mark_read(db(), session["uid"], nid)
        return redirect(request.args.get("to") or url_for("notifications"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = authenticate(db(), request.form["email"].strip(), request.form["password"])
            if user:
                session.clear()
                session["uid"] = user["id"]
                session["email"] = user["email"]
                session["name"] = user["full_name"] or user["email"]
                session["roles"] = list_roles_for(db(), user["id"])
                return redirect(request.args.get("next") or url_for("dashboard"))
            flash("Invalid email or password.", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        conn = db()
        recent = conn.execute(
            """SELECT t.bloom_report_id, t.at, t.action, t.water_body_name, t.determination_code
               FROM (
                   SELECT DISTINCT ON (a.bloom_report_id) a.bloom_report_id, a.at, a.action,
                          w.water_body_name, e.determination_code
                   FROM report_activity a
                   JOIN event e ON e.bloom_report_id = a.bloom_report_id
                   LEFT JOIN location l ON l.id = e.location_id
                   LEFT JOIN waterbody w ON w.id = l.waterbody_id
                   WHERE a.user_id = %s
                   ORDER BY a.bloom_report_id, a.at DESC
               ) t ORDER BY t.at DESC LIMIT 5""", (session["uid"],)).fetchall()
        return render_template("dashboard.html", regions=user_regions(conn, session["uid"]),
                               recent=recent)

    @app.route("/reports/go")
    @login_required
    def report_go():
        brid = (request.args.get("brid") or "").strip()
        if brid.isdigit():
            return redirect(url_for("report_detail", brid=int(brid)))
        flash("Enter a report ID to update.", "error")
        return redirect(url_for("dashboard"))

    @app.route("/reports")
    @login_required
    def reports():
        conn = db()
        with acting_as(conn, session["uid"]):
            rows = conn.execute(
                """SELECT e.bloom_report_id, w.water_body_name, w.regional_water_board,
                          e.observation_date, e.event_status, e.report_type, e.determination_code
                   FROM event e
                   LEFT JOIN location l ON l.id = e.location_id
                   LEFT JOIN waterbody w ON w.id = l.waterbody_id
                   ORDER BY e.bloom_report_id DESC LIMIT 100"""
            ).fetchall()
        return render_template("reports.html", rows=rows, determinations=_determinations())

    @app.route("/reports/<int:brid>/determination", methods=["POST"])
    @login_required
    def set_determination(brid):
        conn = db()
        code = (request.form.get("determination_code") or "").strip() or None
        try:
            with acting_as(conn, session["uid"]):
                conn.execute(
                    "UPDATE event SET determination_code = %s WHERE bloom_report_id = %s",
                    (code, brid))
                conn.commit()
            _record_activity(brid, "set outcome")
            flash(f"Outcome updated for report {brid}.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            flash("Access denied: you may not update that report.", "error")
        except psycopg.Error as exc:
            conn.rollback()
            flash("Could not update: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("reports"))

    @app.route("/reports/<int:brid>")
    @login_required
    def report_detail(brid):
        conn = db()
        with acting_as(conn, session["uid"]):
            ev = conn.execute(
                """SELECT e.bloom_report_id, e.observation_date, e.report_type, e.event_status,
                          e.determination_code, e.bloom_type, e.bloom_size, e.bloom_location,
                          e.bloom_texture, e.bloom_textures, e.surface_water_condition,
                          e.weather_condition, e.signs_posted, e.has_pictures,
                          e.bloom_description, e.management_comments, e.no_illness_observed,
                          e.illness_description, e.reporter_name, e.reporter_email,
                          e.reporter_phone, e.reporter_org, e.case_id,
                          l.landmark, w.water_body_name, w.regional_water_board, w.county
                   FROM event e
                   LEFT JOIN location l ON l.id = e.location_id
                   LEFT JOIN waterbody w ON w.id = l.waterbody_id
                   WHERE e.bloom_report_id = %s""", (brid,)).fetchone()
            if not ev:
                flash("Report not found or not visible to your role.", "error")
                return redirect(url_for("reports"))
            illness = conn.execute(
                "SELECT subject, illness, death FROM report_illness WHERE bloom_report_id = %s",
                (brid,)).fetchall()
            illness_map = {r["subject"]: r for r in illness}
            photos = conn.execute(
                """SELECT id, filename, content_type, uploaded_at FROM report_photo
                   WHERE bloom_report_id = %s ORDER BY uploaded_at""", (brid,)).fetchall()
            # Reporter PII shows only to internal staff (illness/photos are already RLS-gated).
            can_see_pii = conn.execute("SELECT fhab_is_internal() AS x").fetchone()["x"]
            results = conn.execute(
                """SELECT r.data_type, r.measurement_value, r.measurement_unit, r.method,
                          r.res_qual_code, r.taxa, s.sample_date, s.sample_id, s.site,
                          s.collected_by, an.analyte, an.analysis_type
                   FROM result r JOIN sample s ON s.id = r.sample_id
                   LEFT JOIN analyte an ON an.id = r.analyte_id
                   WHERE s.bloom_report_id = %s ORDER BY s.sample_date DESC NULLS LAST""",
                (brid,)).fetchall()
            responses = conn.execute(
                """SELECT r.response_action_id, r.response_category, r.response_type,
                          a.advisory_recommended, a.advisory_start_date, a.advisory_end_date,
                          a.display_advisory_on_map
                   FROM response r LEFT JOIN advisory a ON a.response_action_id = r.response_action_id
                   WHERE r.bloom_report_id = %s ORDER BY r.response_action_id""", (brid,)).fetchall()
            case = None
            if ev["case_id"]:
                case = conn.execute(
                    """SELECT case_id, case_class, case_status, case_lead, case_year,
                              case_start_date, case_end_date
                       FROM hab_case WHERE case_id = %s""", (ev["case_id"],)).fetchone()
            locations = report_locations(conn, brid)
        return render_template("report_detail.html", ev=ev, results=results, responses=responses,
                               case=case, locations=locations, illness_map=illness_map,
                               photos=photos, can_see_pii=can_see_pii,
                               determinations=_determinations(), analytes=_analytes(),
                               data_types=DATA_TYPES, recommended_advisories=_recommended_advisories(),
                               response_categories=RESPONSE_CATEGORIES, **VOCAB)

    @app.route("/reports/<int:brid>/responses", methods=["POST"])
    @login_required
    def add_report_response(brid):
        conn, f = db(), request.form
        try:
            add_response(
                conn, session["uid"], brid,
                response_category=(f.get("response_category") or "Advisory").strip(),
                updated_by=session.get("email"),
                advisory_recommended=(f.get("advisory_recommended") or "").strip() or None,
                advisory_detail=(f.get("advisory_detail") or "").strip() or None,
                advisory_start_date=(f.get("advisory_start_date") or "").strip() or None,
                advisory_end_date=(f.get("advisory_end_date") or "").strip() or None,
                display_advisory_on_map=bool(f.get("display_advisory_on_map")),
            )
            _record_activity(brid, "recorded response")
            flash("Response recorded.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            flash("Access denied: only staff may record responses or post advisories.", "error")
        except psycopg.Error as exc:
            conn.rollback()
            flash("Could not record response: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("report_detail", brid=brid))

    def _save_upload(fileobj):
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        fileobj.save(tmp.name)
        tmp.close()
        return tmp.name

    def _fetch_to_temp(url):
        import subprocess
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        tmp.close()
        subprocess.run(["curl", "-fsSL", "-o", tmp.name, url], check=True,
                       capture_output=True, timeout=60)
        return tmp.name

    @app.route("/batch/ceden", methods=["GET", "POST"])
    @staff_required
    def batch_ceden():
        conn = db()
        if request.method == "POST":
            url = (request.form.get("url") or "").strip()
            chem = request.files.get("chem_file")
            field = request.files.get("field_file")
            tmps = []
            try:
                if url:
                    chem_path, field_path = _fetch_to_temp(url), None
                    tmps.append(chem_path)
                    source = url
                elif chem and chem.filename:
                    chem_path = _save_upload(chem); tmps.append(chem_path)
                    field_path = None
                    if field and field.filename:
                        field_path = _save_upload(field); tmps.append(field_path)
                    source = chem.filename
                else:
                    flash("Provide a CEDEN WaterChemistry CSV (upload) or an API URL.", "error")
                    return redirect(url_for("batch_ceden"))
                rep = load_ceden_output(conn, field_path, chem_path, link=True).counts
                flash(f"Ingested {rep.get('results', 0)} result(s) across "
                      f"{rep.get('samples', 0)} sample(s) / {rep.get('stations', 0)} station(s) "
                      f"from {source}; linked {rep.get('event_links', 0)} sample(s) to events.", "ok")
            except Exception as exc:  # noqa: BLE001 - surface fetch/parse/load errors to staff
                conn.rollback()
                flash("CEDEN ingest failed: " + str(exc).splitlines()[0], "error")
            finally:
                for t in tmps:
                    try:
                        os.unlink(t)
                    except OSError:
                        pass
            return redirect(url_for("batch_ceden"))
        return render_template("batch_ceden.html")

    @app.route("/batch", methods=["GET", "POST"])
    @staff_required
    def batch_determination():
        conn = db()
        if request.method == "POST":
            ids = [int(x) for x in request.form.getlist("report_ids") if x.isdigit()]
            code = (request.form.get("determination_code") or "").strip() or None
            if ids:
                try:
                    with acting_as(conn, session["uid"]):
                        for brid in ids:
                            conn.execute(
                                "UPDATE event SET determination_code = %s WHERE bloom_report_id = %s",
                                (code, brid))
                        conn.commit()
                    flash(f"Updated outcome for {len(ids)} report(s).", "ok")
                except psycopg.Error as exc:
                    conn.rollback()
                    flash("Could not update: " + str(exc).splitlines()[0], "error")
            else:
                flash("No reports selected.", "error")
            return redirect(url_for("batch_determination", case=request.form.get("case") or None,
                                    outcome=request.form.get("outcome") or None))

        case_filter = (request.args.get("case") or "").strip()
        outcome_filter = (request.args.get("outcome") or "").strip()
        conds, params = [], []
        if case_filter.isdigit():
            conds.append("e.case_id = %s"); params.append(int(case_filter))
        if outcome_filter == "__none__":
            conds.append("e.determination_code IS NULL")
        elif outcome_filter:
            conds.append("e.determination_code = %s"); params.append(outcome_filter)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        conn = db()
        with acting_as(conn, session["uid"]):
            rows = conn.execute(
                f"""SELECT e.bloom_report_id, w.water_body_name, w.regional_water_board,
                           e.determination_code, e.case_id
                    FROM event e LEFT JOIN location l ON l.id = e.location_id
                    LEFT JOIN waterbody w ON w.id = l.waterbody_id
                    {where}
                    ORDER BY e.bloom_report_id DESC LIMIT 200""", params).fetchall()
        return render_template("batch.html", rows=rows, determinations=_determinations(),
                               case_filter=case_filter, outcome_filter=outcome_filter)

    @app.route("/map")
    @login_required
    def report_map():
        return render_template("map.html")

    @app.route("/api/reports.geojson")
    @login_required
    def reports_geojson():
        conn = db()
        with acting_as(conn, session["uid"]):
            # Kept lean for the free-tier DB: one lateral for the displayed advisory, no
            # per-row count subqueries (the detail page carries the full picture).
            rows = conn.execute(
                """SELECT e.bloom_report_id, ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon,
                          w.water_body_name, w.regional_water_board, e.observation_date::text AS obs,
                          e.event_status, e.determination_code, rd.label AS det_label, e.case_id,
                          adv.advisory_recommended AS advisory
                   FROM event e
                   JOIN location l ON l.id = e.location_id AND l.geom IS NOT NULL
                   LEFT JOIN waterbody w ON w.id = l.waterbody_id
                   LEFT JOIN report_determination rd ON rd.code = e.determination_code
                   LEFT JOIN LATERAL (
                       SELECT a.advisory_recommended
                       FROM response r JOIN advisory a ON a.response_action_id = r.response_action_id
                       WHERE r.bloom_report_id = e.bloom_report_id AND a.display_advisory_on_map
                       ORDER BY a.advisory_start_date DESC NULLS LAST LIMIT 1
                   ) adv ON true
                   ORDER BY e.bloom_report_id DESC LIMIT 2000"""
            ).fetchall()
        props = ("bloom_report_id", "water_body_name", "regional_water_board", "obs",
                 "event_status", "determination_code", "det_label", "case_id", "advisory")
        features = [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {k: r[k] for k in props},
        } for r in rows]
        return jsonify({"type": "FeatureCollection", "features": features})

    @app.route("/reports/<int:brid>/edit", methods=["POST"])
    @login_required
    def edit_report(brid):
        conn, f = db(), request.form
        try:
            update_report(
                conn, session["uid"], brid,
                observation_date=(f.get("date") or "").strip() or None,
                bloom_type=(f.get("bloom_type") or "").strip() or None,
                bloom_size=(f.get("bloom_size") or "").strip() or None,
                bloom_location=(f.get("bloom_location") or "").strip() or None,
                bloom_texture=(f.get("bloom_texture") or "").strip() or None,
                bloom_textures=f.getlist("bloom_textures") or None,
                surface_water_condition=(f.get("surface_water_condition") or "").strip() or None,
                weather_condition=(f.get("weather_condition") or "").strip() or None,
                signs_posted=(f.get("signs_posted") or "").strip() or None,
                has_pictures=_yn(f.get("has_pictures")),
                bloom_description=(f.get("bloom_description") or "").strip() or None,
                management_comments=(f.get("management_comments") or "").strip() or None,
                determination=(f.get("determination_code") or "").strip() or None,
            )
            _record_activity(brid, "edited report")
            flash("Report updated.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: you may not edit that report.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not update: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("report_detail", brid=brid))

    @app.route("/reports/<int:brid>/illness", methods=["POST"])
    @staff_required
    def update_report_illness(brid):
        conn, f = db(), request.form
        try:
            set_report_illness(conn, session["uid"], brid, rows=_illness_rows(f),
                               none_observed=bool(f.get("no_illness_observed")),
                               description=(f.get("illness_description") or "").strip() or None)
            _record_activity(brid, "updated illness report")
            flash("Suspected illness/death updated.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not update: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("report_detail", brid=brid))

    @app.route("/reports/<int:brid>/photos", methods=["POST"])
    @staff_required
    def upload_report_photo(brid):
        conn = db()
        upload = request.files.get("photo")
        if not upload or not upload.filename:
            flash("Choose an image to upload.", "error")
            return redirect(url_for("report_detail", brid=brid))
        data = upload.read()
        if len(data) > 8 * 1024 * 1024:
            flash("Image too large (max 8 MB).", "error")
            return redirect(url_for("report_detail", brid=brid))
        try:
            with acting_as(conn, session["uid"]):
                conn.execute(
                    """INSERT INTO report_photo
                         (bloom_report_id, filename, content_type, data, uploaded_by)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (brid, upload.filename, upload.mimetype, data, session["uid"]))
                conn.commit()
            _record_activity(brid, "added photo")
            flash("Photo uploaded.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: only staff may upload photos.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not upload: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("report_detail", brid=brid))

    @app.route("/reports/<int:brid>/photos/<int:pid>")
    @login_required
    def serve_report_photo(brid, pid):
        from flask import Response
        conn = db()
        with acting_as(conn, session["uid"]):
            row = conn.execute(
                "SELECT content_type, data FROM report_photo WHERE id = %s AND bloom_report_id = %s",
                (pid, brid)).fetchone()
        if not row:
            flash("Photo not found or not visible to your role.", "error")
            return redirect(url_for("report_detail", brid=brid))
        return Response(bytes(row["data"]), mimetype=row["content_type"] or "application/octet-stream")

    @app.route("/reports/<int:brid>/results", methods=["POST"])
    @login_required
    def add_report_result(brid):
        conn, f = db(), request.form
        try:
            add_result(
                conn, session["uid"], brid,
                data_type=f["data_type"],
                sample_date=(f.get("sample_date") or "").strip() or None,
                analyte_id=int(f["analyte_id"]) if f.get("analyte_id") else None,
                measurement_value=_f(f.get("measurement_value")),
                measurement_unit=(f.get("measurement_unit") or "").strip() or None,
                method=(f.get("method") or "").strip() or None,
                res_qual_code=(f.get("res_qual_code") or "").strip() or None,
                taxa=(f.get("taxa") or "").strip() or None,
                collected_by=(f.get("collected_by") or "").strip() or None,
                sample_label=(f.get("sample_label") or "").strip() or None,
                site=(f.get("site") or "").strip() or None,
            )
            _record_activity(brid, "added result")
            flash("Result added.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: you may not add results to that report.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not add result: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("report_detail", brid=brid))

    @app.route("/reports/<int:brid>/lab-upload", methods=["POST"])
    @staff_required
    def upload_lab_results(brid):
        conn = db()
        upload = request.files.get("chem_file")
        if not upload or not upload.filename:
            flash("Choose a CEDEN WaterChemistry CSV to upload.", "error")
            return redirect(url_for("report_detail", brid=brid))
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        try:
            upload.save(tmp.name)
            tmp.close()
            rep = load_chemistry_for_event(conn, brid, tmp.name, session["uid"])
            _record_activity(brid, "uploaded lab results")
            flash(f"Uploaded {rep.counts['results']} lab result(s) across "
                  f"{rep.counts['samples']} sample(s).", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: only staff may upload lab results.", "error")
        except Exception as exc:  # noqa: BLE001 - surface parse/load errors to the user
            conn.rollback()
            flash("Upload failed (expecting a CEDEN WaterChemistry CSV): "
                  + str(exc).splitlines()[0], "error")
        finally:
            os.unlink(tmp.name)
        return redirect(url_for("report_detail", brid=brid))

    @app.route("/reports/new", methods=["GET", "POST"])
    @login_required
    def new_report():
        conn = db()
        if request.method == "POST":
            f = request.form
            region = (f.get("region") or "").strip() or None
            regs = user_regions(conn, session["uid"])
            cross = bool(region and regs and region not in regs)
            if cross and not f.get("confirm_cross"):
                return render_template("new_report.html", form=f, regions=_regions(),
                                       determinations=_determinations(), cross_warn=(regs, region),
                                       wb_suggestions=None, **VOCAB)
            # Controlled-vocabulary guard for waterbody: if the typed name has no exact match but
            # is close to existing ones, ask the staffer to pick the canonical name or confirm new.
            wb_name = f["waterbody"].strip()
            county = (f.get("county") or "").strip() or None
            exact = conn.execute(
                "SELECT 1 FROM waterbody WHERE lower(water_body_name)=lower(%s)", (wb_name,)).fetchone()
            if not exact and not f.get("confirm_new_wb"):
                sims = similar_waterbodies(conn, wb_name, county)
                if sims:
                    return render_template("new_report.html", form=f, regions=_regions(),
                                           determinations=_determinations(), cross_warn=None,
                                           wb_suggestions=sims, **VOCAB)
            try:
                rid = enter_report(
                    conn, session["uid"],
                    water_body_name=f["waterbody"].strip(), region=region,
                    county=(f.get("county") or "").strip() or None,
                    landmark=(f.get("landmark") or "").strip() or None,
                    lat=_f(f.get("lat")), lon=_f(f.get("lon")),
                    observation_date=(f.get("date") or "").strip() or None,
                    report_type=(f.get("report_type") or "Public Reporting").strip(),
                    bloom_type=(f.get("bloom_type") or "").strip() or None,
                    bloom_size=(f.get("bloom_size") or "").strip() or None,
                    bloom_location=(f.get("bloom_location") or "").strip() or None,
                    bloom_textures=f.getlist("bloom_textures") or None,
                    surface_water_condition=(f.get("surface_water_condition") or "").strip() or None,
                    weather_condition=(f.get("weather_condition") or "").strip() or None,
                    signs_posted=(f.get("signs_posted") or "").strip() or None,
                    has_pictures=_yn(f.get("has_pictures")),
                    description=(f.get("description") or "").strip() or None,
                    management_comments=(f.get("management_comments") or "").strip() or None,
                    reporter_name=(f.get("reporter_name") or "").strip() or None,
                    reporter_email=(f.get("reporter_email") or "").strip() or None,
                    reporter_phone=(f.get("reporter_phone") or "").strip() or None,
                    reporter_org=(f.get("reporter_org") or "").strip() or None,
                    determination=(f.get("determination_code") or "").strip() or None,
                )
                set_report_illness(conn, session["uid"], rid,
                                   rows=_illness_rows(f), none_observed=bool(f.get("no_illness_observed")),
                                   description=(f.get("illness_description") or "").strip() or None)
                _record_activity(rid, "entered report")
                flash(f"Report entered — Bloom_Report_ID {rid}.", "ok")
                return redirect(url_for("report_detail", brid=rid))
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                flash("Access denied: your role may not file this report.", "error")
            except psycopg.Error as exc:
                conn.rollback()
                flash("Could not enter report: " + str(exc).splitlines()[0], "error")
        return render_template("new_report.html", form={}, regions=_regions(),
                               determinations=_determinations(), cross_warn=None,
                               wb_suggestions=None, **VOCAB)

    @app.route("/api/waterbodies")
    @login_required
    def api_waterbodies():
        return jsonify(suggest_waterbodies(db(), request.args.get("q", "")))

    # ---------- Lab results browser ----------
    def _lab_filters(args):
        f = {k: (args.get(k) or "").strip() or None
             for k in ("analysis_type", "analyte", "region", "data_type", "q",
                       "date_from", "date_to", "nd")}
        sort = args.get("sort") if args.get("sort") in ("date", "value", "waterbody", "analyte") else "date"
        desc = args.get("dir", "desc") != "asc"
        return f, sort, desc

    @app.route("/lab")
    @staff_required
    def lab_results():
        f, sort, desc = _lab_filters(request.args)
        per = 100
        try:
            page = max(0, int(request.args.get("page", 0)))
        except ValueError:
            page = 0
        conn = db()
        rows = query_results(conn, f, sort=sort, desc=desc, limit=per, offset=page * per)
        total = count_results(conn, f)
        base_args = {k: v for k, v in request.args.items() if k != "page"}
        return render_template("lab_results.html", rows=rows, total=total, page=page, per=per,
                               f=f, sort=sort, desc=desc, options=filter_options(conn),
                               data_types=DATA_TYPES, base_args=base_args)

    @app.route("/lab.csv")
    @staff_required
    def lab_results_csv():
        from flask import Response
        f, sort, desc = _lab_filters(request.args)
        rows = query_results(db(), f, sort=sort, desc=desc, limit=50000, offset=0)
        cols = ["sample_date", "water_body_name", "regional_water_board", "county",
                "bloom_report_id", "analysis_type", "analyte_class", "analyte", "data_type",
                "measurement_value", "measurement_text", "measurement_unit", "res_qual_code",
                "method", "mdl", "rl", "site"]
        out = _csv_text([c.title() for c in cols], [{c.title(): r[c] for c in cols} for r in rows])
        stamp = __import__("datetime").date.today().isoformat()
        resp = Response(out, mimetype="text/csv")
        resp.headers["Content-Disposition"] = f'attachment; filename="fhab_results_{stamp}.csv"'
        return resp

    # ---------- Public submission API (external apps, e.g. the CyanoSafe phone demo) ----------
    def _cors(resp, origin):
        allow = origin if origin in PUBLIC_ORIGINS else (PUBLIC_ORIGINS[0] if PUBLIC_ORIGINS else "*")
        resp.headers["Access-Control-Allow-Origin"] = allow
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
        return resp

    @app.route("/api/public/reports", methods=["POST", "OPTIONS"])
    def public_submit():
        origin = request.headers.get("Origin", "")
        if request.method == "OPTIONS":
            return _cors(app.make_response(("", 204)), origin)

        def fail(msg, code):
            r = _cors(jsonify({"ok": False, "error": msg}), origin)
            r.status_code = code
            return r

        # A key may identify a registered community/partner group; otherwise it's anonymous
        # public (gated by PUBLIC_KEY if one is configured).
        key = request.headers.get("X-API-Key")
        from ..intake import TIER_REPORT_TYPE
        group = resolve_intake_group(db(), key) if key else None
        if not group and PUBLIC_KEY and key != PUBLIC_KEY:
            return fail("unauthorized", 401)
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "?")
        if not _rate_ok(ip):
            return fail("rate limit exceeded; try again later", 429)
        payload = request.get_json(silent=True) or {}
        if (payload.get("website") or "").strip():   # honeypot — silently accept & discard
            return _cors(jsonify({"ok": True, "id": None}), origin)
        # report_type / group / trusted come from the authenticated group, never the payload.
        kw = (dict(source=group["group_name"], report_type=TIER_REPORT_TYPE.get(group["tier"]),
                   group_id=group["id"], trusted=bool(group["trusted"]))
              if group else dict(source=(payload.get("source") or "api")))
        try:
            sid = submit_public_report(db(), payload, remote_ip=ip, **kw)
        except SubmissionError as exc:
            return fail(str(exc), 400)
        except Exception:  # noqa: BLE001
            db().rollback()
            return fail("could not accept submission", 400)
        try:  # best-effort: notify reviewers (and escalate suspected illness). Never blocks the 200.
            row = db().execute(
                "SELECT water_body_name, (illness IS NOT NULL) AS has_illness "
                "FROM public_report_submission WHERE id=%s", (sid,)).fetchone()
            on_new_submission(db(), sid, water_body=row["water_body_name"],
                              has_illness=row["has_illness"], source=kw.get("source"))
        except Exception:  # noqa: BLE001
            db().rollback()
        return _cors(jsonify({"ok": True, "id": sid,
                              "message": "Thank you — your report was received for review."}), origin)

    @app.route("/intake/review")
    @staff_required
    def intake_review():
        status = request.args.get("status", "pending")
        trusted_only = request.args.get("trusted") == "1"
        subs = list_submissions(db(), session["uid"], status, trusted_only=trusted_only)
        return render_template("intake_review.html", subs=subs, status=status,
                               trusted_only=trusted_only, regions=_regions())

    @app.route("/intake/promote-trusted", methods=["POST"])
    @staff_required
    def intake_promote_trusted():
        conn = db()
        try:
            n = promote_trusted_pending(conn, session["uid"])
            flash(f"Promoted {n} trusted submission(s).", "ok")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not promote: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("intake_review"))

    @app.route("/intake/<int:sid>/promote", methods=["POST"])
    @staff_required
    def intake_promote(sid):
        conn = db()
        try:
            brid = promote_submission(conn, session["uid"], sid,
                                      region=(request.form.get("region") or "").strip() or None)
            _record_activity(brid, "promoted public submission")
            flash(f"Promoted to report {brid}.", "ok")
            return redirect(url_for("report_detail", brid=brid))
        except SubmissionError as exc:
            flash(str(exc), "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not promote: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("intake_review"))

    @app.route("/intake/<int:sid>/reject", methods=["POST"])
    @staff_required
    def intake_reject(sid):
        reject_submission(db(), session["uid"], sid,
                          note=(request.form.get("note") or "").strip() or None)
        flash("Submission rejected.", "ok")
        return redirect(url_for("intake_review"))

    @app.route("/intake/<int:sid>/photo")
    @staff_required
    def intake_photo(sid):
        from flask import Response
        conn = db()
        with acting_as(conn, session["uid"]):
            row = conn.execute(
                "SELECT photo, photo_content_type FROM public_report_submission WHERE id=%s",
                (sid,)).fetchone()
        if not row or not row["photo"]:
            flash("No photo on that submission.", "error")
            return redirect(url_for("intake_review"))
        return Response(bytes(row["photo"]), mimetype=row["photo_content_type"] or "image/jpeg")

    # ---------- Community/partner intake groups (API keys) — admin ----------
    @app.route("/admin/intake-groups", methods=["GET", "POST"])
    @admin_required
    def admin_intake_groups():
        conn = db()
        if request.method == "POST":
            name = (request.form.get("group_name") or "").strip()
            if not name:
                flash("Enter a group name.", "error")
                return redirect(url_for("admin_intake_groups"))
            try:
                _, key = create_intake_group(
                    conn, session["uid"], name,
                    tier=(request.form.get("tier") or "community"),
                    trusted=bool(request.form.get("trusted")))
                # The key is shown once — staff must copy it now.
                flash(f"Group “{name}” created. API key (copy now, shown once): {key}", "ok")
            except SubmissionError as exc:
                flash(str(exc), "error")
            except psycopg.Error as exc:
                conn.rollback(); flash("Could not create: " + str(exc).splitlines()[0], "error")
            return redirect(url_for("admin_intake_groups"))
        return render_template("intake_groups.html", groups=list_intake_groups(conn, session["uid"]))

    @app.route("/admin/intake-groups/<int:gid>/active", methods=["POST"])
    @admin_required
    def admin_intake_group_active(gid):
        set_group_active(db(), session["uid"], gid, request.form.get("active") == "1")
        flash("Group updated.", "ok")
        return redirect(url_for("admin_intake_groups"))

    # ---------- Open-data flat files (the four data.ca.gov files) ----------
    @app.route("/export")
    @staff_required
    def export_index():
        conn = db()
        files = []
        for slug, (title, desc) in DATASETS.items():
            _, records = fetch_flatfile(conn, slug)
            files.append({"slug": slug, "title": title, "desc": desc, "count": len(records)})
        return render_template("export.html", files=files)

    @app.route("/export/<slug>.csv")
    @staff_required
    def export_csv(slug):
        from flask import Response
        if slug not in DATASETS:
            flash("Unknown dataset.", "error")
            return redirect(url_for("export_index"))
        headers, records = fetch_flatfile(db(), slug)
        stamp = __import__("datetime").date.today().isoformat()
        resp = Response(_csv_text(headers, records), mimetype="text/csv")
        resp.headers["Content-Disposition"] = f'attachment; filename="fhab_{slug}_{stamp}.csv"'
        return resp

    @app.route("/export/all.zip")
    @staff_required
    def export_zip():
        import io
        import zipfile
        from flask import Response
        conn = db()
        stamp = __import__("datetime").date.today().isoformat()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for slug in DATASETS:
                headers, records = fetch_flatfile(conn, slug)
                zf.writestr(f"fhab_{slug}_{stamp}.csv", _csv_text(headers, records))
        resp = Response(buf.getvalue(), mimetype="application/zip")
        resp.headers["Content-Disposition"] = f'attachment; filename="fhab_flatfiles_{stamp}.zip"'
        return resp

    # Public provisional open-data API (read-only, CORS-open). Same published columns as the
    # CSVs — no reporter PII / illness, veterinary excluded — but reflects the live (provisional)
    # database, including reports not yet in an official data.ca.gov release.
    def _open_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "public, max-age=600"
        return resp

    @app.route("/api/open/index.json")
    def open_index():
        base = request.host_url.rstrip("/")
        datasets = [{
            "slug": slug, "title": title, "description": desc,
            "json": f"{base}/api/open/{slug}.json", "csv": f"{base}/export/{slug}.csv",
        } for slug, (title, desc) in DATASETS.items()]
        return _open_cors(jsonify({
            "provisional": True,
            "notice": ("Provisional FHAB data from the live database; not the official "
                       "data.ca.gov release. Subject to change as reports are verified."),
            "source": "https://data.ca.gov/dataset/surface-water-freshwater-harmful-algal-blooms",
            "datasets": datasets,
        }))

    @app.route("/api/open/<slug>.json")
    def open_dataset(slug):
        if slug not in DATASETS:
            return _open_cors(jsonify({"error": "unknown dataset"})), 404
        now = _time.time()
        cached = None if app.testing else _OPEN_CACHE.get(slug)
        if not cached or now - cached[0] > _OPEN_TTL:
            headers, records = fetch_flatfile(db(), slug)
            records = [{k: _jsonable(v) for k, v in rec.items()} for rec in records]
            payload = {"provisional": True, "dataset": slug,
                       "title": DATASETS[slug][0],
                       "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                       "count": len(records), "records": records}
            _OPEN_CACHE[slug] = (now, payload)
            cached = _OPEN_CACHE[slug]
        return _open_cors(jsonify(cached[1]))

    # ---------- Lab batch reconciliation ----------
    @app.route("/batch/lab-reconcile", methods=["GET", "POST"])
    @staff_required
    def lab_reconcile():
        conn = db()
        if request.method == "POST":
            upload = request.files.get("batch_file")
            if not upload or not upload.filename:
                flash("Choose a CEDEN chemistry template CSV.", "error")
                return redirect(url_for("lab_reconcile"))
            try:
                radius = int(request.form.get("radius_m") or 2000)
                days = int(request.form.get("days") or 14)
            except ValueError:
                radius, days = 2000, 14
            tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
            try:
                upload.save(tmp.name); tmp.close()
                bid = stage_batch(conn, session["uid"], tmp.name,
                                  filename=upload.filename, radius_m=radius, days=days)
                flash("Batch staged. Review and link the sample groups below.", "ok")
                return redirect(url_for("lab_batch", bid=bid))
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                flash("Could not stage batch (expecting a CEDEN chemistry template CSV): "
                      + str(exc).splitlines()[0], "error")
            finally:
                os.unlink(tmp.name)
        with acting_as(conn, session["uid"]):
            batches = conn.execute(
                """SELECT b.*,
                          (SELECT count(*) FROM lab_stage_sample s WHERE s.batch_id=b.id AND s.status='linked') AS linked,
                          (SELECT count(*) FROM lab_stage_sample s WHERE s.batch_id=b.id AND s.status='unmatched') AS unmatched
                   FROM lab_batch b ORDER BY b.id DESC LIMIT 50""").fetchall()
        return render_template("lab_reconcile.html", batches=batches)

    @app.route("/batch/lab-reconcile/<int:bid>")
    @staff_required
    def lab_batch(bid):
        conn = db()
        with acting_as(conn, session["uid"]):
            batch = conn.execute("SELECT * FROM lab_batch WHERE id=%s", (bid,)).fetchone()
            if not batch:
                flash("Batch not found.", "error")
                return redirect(url_for("lab_reconcile"))
            samples = conn.execute(
                """SELECT ss.id, ss.station_code, ss.sample_date, ss.status, ss.linked_event,
                          (st.geom IS NOT NULL) AS has_geom,
                          (SELECT count(*) FROM lab_stage_result r WHERE r.stage_sample_id=ss.id) AS n_results
                   FROM lab_stage_sample ss LEFT JOIN station st ON st.id=ss.station_id
                   WHERE ss.batch_id=%s
                   ORDER BY (ss.status='unmatched') DESC, ss.station_code, ss.sample_date""",
                (bid,)).fetchall()
        return render_template("lab_batch.html", batch=batch, samples=samples)

    @app.route("/batch/lab-reconcile/<int:bid>/auto", methods=["POST"])
    @staff_required
    def lab_batch_auto(bid):
        conn = db()
        try:
            n = auto_match(conn, session["uid"], bid)
            flash(f"Auto-linked {n} high-confidence group(s). Review the rest below.", "ok")
        except psycopg.Error as exc:
            conn.rollback(); flash("Auto-match failed: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("lab_batch", bid=bid))

    @app.route("/batch/lab-reconcile/<int:bid>/sample/<int:sid>")
    @staff_required
    def lab_stage(bid, sid):
        conn = db()
        with acting_as(conn, session["uid"]):
            ss = conn.execute(
                """SELECT ss.*, st.station_name, (st.geom IS NOT NULL) AS has_geom,
                          ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon
                   FROM lab_stage_sample ss LEFT JOIN station st ON st.id=ss.station_id
                   WHERE ss.id=%s""", (sid,)).fetchone()
            if not ss:
                flash("Sample group not found.", "error")
                return redirect(url_for("lab_batch", bid=bid))
            results = conn.execute(
                """SELECT analyte_name, method_name, result, res_qual_code, unit_name
                   FROM lab_stage_result WHERE stage_sample_id=%s ORDER BY analyte_name""",
                (sid,)).fetchall()
            batch = conn.execute("SELECT match_radius_m, match_days FROM lab_batch WHERE id=%s",
                                 (bid,)).fetchone()
            cands = _candidates(conn, sid, radius_m=batch["match_radius_m"],
                                days=batch["match_days"], limit=5)
        return render_template("lab_stage.html", bid=bid, ss=ss, results=results, cands=cands,
                               regions=_regions())

    @app.route("/lab-reconcile/sample/<int:sid>/link", methods=["POST"])
    @staff_required
    def lab_stage_link(sid):
        conn, f = db(), request.form
        ev = (f.get("bloom_report_id") or "").strip()
        case = (f.get("case_id") or "").strip()
        if not ev.isdigit() and not case.isdigit():
            flash("Enter a report ID or a case ID to link to.", "error")
            return redirect(url_for("lab_stage", bid=f.get("bid"), sid=sid))
        try:
            link_stage_sample(conn, session["uid"], sid,
                              bloom_report_id=int(ev) if ev.isdigit() else None,
                              case_id=int(case) if case.isdigit() else None)
            if ev.isdigit():
                _record_activity(int(ev), "linked lab batch sample")
            flash("Lab data linked to the " + ("report." if ev.isdigit() else "case."), "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not link: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("lab_batch", bid=f.get("bid")))

    @app.route("/lab-reconcile/sample/<int:sid>/create-event", methods=["POST"])
    @staff_required
    def lab_stage_create_event(sid):
        conn, f = db(), request.form
        try:
            brid = create_event_from_stage(conn, session["uid"], sid,
                                           region=(f.get("region") or "").strip() or None)
            _record_activity(brid, "created report from lab batch")
            flash(f"Created report {brid} from the station and linked the lab data.", "ok")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not create report: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("lab_batch", bid=f.get("bid")))

    @app.route("/lab-reconcile/sample/<int:sid>/skip", methods=["POST"])
    @staff_required
    def lab_stage_skip(sid):
        conn = db()
        skip_stage_sample(conn, session["uid"], sid)
        flash("Sample group set aside.", "ok")
        return redirect(url_for("lab_batch", bid=request.form.get("bid")))

    # ---------- Cases ----------
    @app.route("/cases")
    @login_required
    def cases():
        conn = db()
        status_f = (request.args.get("status") or "").strip()
        region_f = (request.args.get("region") or "").strip()
        year_f = (request.args.get("year") or "").strip()
        conds, params = [], []
        if status_f:
            conds.append("c.case_status = %s"); params.append(status_f)
        if region_f:
            conds.append("w.regional_water_board = %s"); params.append(region_f)
        if year_f.isdigit():
            conds.append("c.case_year = %s"); params.append(int(year_f))
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        with acting_as(conn, session["uid"]):
            rows = conn.execute(
                f"""SELECT c.case_id, c.case_water_body_name, c.case_status, c.case_lead,
                           c.case_year, w.regional_water_board,
                           (SELECT count(*) FROM event e WHERE e.case_id = c.case_id) AS reports
                    FROM hab_case c LEFT JOIN waterbody w ON w.id = c.waterbody_id
                    {where}
                    ORDER BY c.case_year DESC NULLS LAST, c.case_id DESC LIMIT 200""", params).fetchall()
        return render_template("cases.html", rows=rows, statuses=CASE_STATUSES, regions=_regions(),
                               status_f=status_f, region_f=region_f, year_f=year_f)

    @app.route("/cases/new", methods=["GET", "POST"])
    @staff_required
    def case_new():
        conn, f = db(), request.form
        if request.method == "POST":
            try:
                cid = create_case(
                    conn, session["uid"],
                    water_body_name=f["waterbody"].strip(),
                    region=(f.get("region") or "").strip() or None,
                    county=(f.get("county") or "").strip() or None,
                    year=int(f["year"]) if f.get("year", "").isdigit() else None,
                    case_class=(f.get("case_class") or "").strip() or None,
                    case_lead=(f.get("case_lead") or "").strip() or None,
                    status=(f.get("status") or "Open").strip())
                flash(f"Case {cid} created.", "ok")
                return redirect(url_for("case_detail", cid=cid))
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                flash("Access denied: you may only create cases in your region.", "error")
            except psycopg.Error as exc:
                conn.rollback(); flash("Could not create case: " + str(exc).splitlines()[0], "error")
        return render_template("case_new.html", regions=_regions(), statuses=CASE_STATUSES)

    @app.route("/cases/<int:cid>")
    @login_required
    def case_detail(cid):
        conn = db()
        with acting_as(conn, session["uid"]):
            case = conn.execute(
                """SELECT c.*, w.regional_water_board, w.county AS wb_county
                   FROM hab_case c LEFT JOIN waterbody w ON w.id = c.waterbody_id
                   WHERE c.case_id = %s""", (cid,)).fetchone()
            if not case:
                flash("Case not found or not visible to your role.", "error")
                return redirect(url_for("cases"))
            reports = conn.execute(
                """SELECT e.bloom_report_id, e.observation_date, e.determination_code,
                          w.water_body_name,
                          (SELECT a.advisory_recommended FROM response r
                             JOIN advisory a ON a.response_action_id = r.response_action_id
                             WHERE r.bloom_report_id = e.bloom_report_id AND a.display_advisory_on_map
                             ORDER BY a.advisory_start_date DESC NULLS LAST LIMIT 1) AS advisory
                   FROM event e LEFT JOIN location l ON l.id = e.location_id
                   LEFT JOIN waterbody w ON w.id = l.waterbody_id
                   WHERE e.case_id = %s ORDER BY e.bloom_report_id""", (cid,)).fetchall()
            locations = case_locations(conn, cid)
        return render_template("case_detail.html", case=case, reports=reports,
                               locations=locations, statuses=CASE_STATUSES)

    @app.route("/cases/<int:cid>/edit", methods=["POST"])
    @staff_required
    def case_edit(cid):
        conn, f = db(), request.form
        try:
            update_case(conn, session["uid"], cid,
                        status=(f.get("status") or "").strip() or None,
                        case_lead=(f.get("case_lead") or "").strip() or None,
                        case_class=(f.get("case_class") or "").strip() or None,
                        year=int(f["year"]) if f.get("year", "").isdigit() else None)
            flash("Case updated.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: you may not edit that case.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not update case: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("case_detail", cid=cid))

    @app.route("/cases/<int:cid>/assign", methods=["POST"])
    @staff_required
    def case_assign(cid):
        conn = db()
        brid = (request.form.get("brid") or "").strip()
        if not brid.isdigit():
            flash("Enter a report ID to assign.", "error")
            return redirect(url_for("case_detail", cid=cid))
        try:
            assign_report_to_case(conn, session["uid"], int(brid), cid)
            _record_activity(int(brid), f"assigned to case {cid}")
            flash(f"Report {brid} assigned to case {cid}.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: you may not assign that report.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not assign: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("case_detail", cid=cid))

    @app.route("/cases/<int:cid>/lab-upload", methods=["POST"])
    @staff_required
    def case_lab_upload(cid):
        conn = db()
        upload = request.files.get("chem_file")
        if not upload or not upload.filename:
            flash("Choose a CEDEN WaterChemistry CSV to upload.", "error")
            return redirect(url_for("case_detail", cid=cid))
        tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        try:
            upload.save(tmp.name); tmp.close()
            rep = load_chemistry_for_case(conn, cid, tmp.name, session["uid"])
            flash(f"Uploaded {rep.counts['results']} lab result(s) across "
                  f"{rep.counts['samples']} sample(s) to the case.", "ok")
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            flash("Upload failed (expecting a CEDEN WaterChemistry CSV): "
                  + str(exc).splitlines()[0], "error")
        finally:
            os.unlink(tmp.name)
        return redirect(url_for("case_detail", cid=cid))

    @app.route("/reports/<int:brid>/assign-case", methods=["POST"])
    @staff_required
    def report_assign_case(brid):
        conn = db()
        cid = (request.form.get("case_id") or "").strip()
        try:
            assign_report_to_case(conn, session["uid"], brid, int(cid) if cid.isdigit() else None)
            _record_activity(brid, f"assigned to case {cid}" if cid else "unassigned from case")
            flash("Case assignment updated." if cid else "Report unassigned from case.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: you may not change that report's case.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not assign: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("report_detail", brid=brid))

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        conn = db()
        users = conn.execute(
            """SELECT u.id, u.email, u.full_name, u.is_active,
                      coalesce(array_agg(ur.role_code) FILTER (WHERE ur.role_code IS NOT NULL), '{}') AS roles
               FROM app_user u LEFT JOIN user_role ur ON ur.user_id = u.id
               GROUP BY u.id ORDER BY u.email"""
        ).fetchall()
        roles = conn.execute("SELECT code, name, category FROM role ORDER BY category, code").fetchall()
        return render_template("users.html", users=users, roles=roles, regions=_regions())

    @app.route("/admin/users/new", methods=["POST"])
    @admin_required
    def admin_create_user():
        conn, f = db(), request.form
        uid = create_user(conn, f["email"].strip(), (f.get("full_name") or "").strip() or None)
        if f.get("password"):
            set_password(conn, uid, f["password"])
        if f.get("role"):
            grant_role(conn, uid, f["role"],
                       region=(f.get("region") or "").strip() or None,
                       org=(f.get("org") or "").strip() or None)
        flash(f"Account created: {f['email']}", "ok")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:uid>/grant", methods=["POST"])
    @admin_required
    def admin_grant(uid):
        f = request.form
        grant_role(db(), uid, f["role"],
                   region=(f.get("region") or "").strip() or None,
                   org=(f.get("org") or "").strip() or None)
        flash("Role granted.", "ok")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:uid>/revoke", methods=["POST"])
    @admin_required
    def admin_revoke(uid):
        revoke_role(db(), uid, request.form["role"])
        flash("Role revoked.", "ok")
        return redirect(url_for("admin_users"))

    return app
