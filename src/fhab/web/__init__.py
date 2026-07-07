"""Staff web app: role-based account management and report data entry (Flask).

Data operations run through the database's Row-Level Security as the logged-in user
(`fhab.auth.acting_as`); account management runs with the privileged connection but is gated
to `program_admin` at the app layer. See docs/USER_ROLES.md.
"""

from __future__ import annotations

import os
import shutil
from functools import wraps

import psycopg
from flask import (Flask, Response, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)

import tempfile

from ..auth import (acting_as, authenticate, create_user, grant_role, list_roles_for,
                    revoke_role, set_password, user_regions)
from ..cases import (CASE_STATUSES, assign_report_to_case, create_case, update_case)
from ..ceden import (load_ceden_output, load_chemistry_for_case, load_chemistry_for_event)
from ..bendlab import (batch_file, batch_files, ingest_bend_folder, ingested_batches)
from ..labmatch import (_candidates, auto_match, create_event_from_stage, link_stage_sample,
                        skip_stage_sample, stage_batch)
from ..places import COUNTIES, similar_waterbodies, suggest_waterbodies
from ..intake import (SubmissionError, create_intake_group, list_intake_groups, list_submissions,
                      promote_submission, promote_trusted_pending, reject_submission,
                      resolve_intake_group, set_group_active, submit_public_report)
from ..export import DATASETS, fetch_flatfile
from ..labquery import count_results, filter_options, query_results
from ..labtasks import (assign_samples, batch_reconcile_samples, bulk_geocode, clear_routine,
                        count_workboard, create_report_from_sample, link_sample,
                        link_sample_stations, link_sample_to_reports, qa_review, sample_geo,
                        set_sample_location, set_sample_point, status_tallies, tag_routine,
                        team_members, unlink_sample, unlink_sample_station, workboard)
from ..ocr import OcrUnavailable, ocr_pdf_coords
from ..refresh import DATASET_URL, RefreshError, refresh_from_ca_gov
from ..samples import count_samples, create_sample, get_sample, list_samples, update_sample
from ..dedup import candidate_duplicate_samples, duplicate_count, merge_samples
from ..maintenance import KEPT_TABLES, LAB_TABLES, lab_data_counts, purge_lab_data
from ..taxonomy import (TaxonomyError, delete_analyte, list_analytes, merge_analytes,
                        update_analyte)
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
        """Expose the nav's unread count + the user's staff/admin flags to every template."""
        roles = set(session.get("roles", []))
        ctx = {"is_staff": bool(roles & STAFF_WRITER_ROLES),
               "is_admin": "program_admin" in roles, "nav_unread": 0}
        if session.get("uid"):
            try:
                ctx["nav_unread"] = unread_count(db(), session["uid"])
            except Exception:  # noqa: BLE001
                pass
        return ctx

    # ---- routes ----
    @app.route("/new")
    @login_required
    def new_reports_home():
        items = [{"title": "Enter a new report", "href": url_for("new_report"),
                  "desc": "File a bloom report (the full MyWaterQuality form)."}]
        if set(session.get("roles", [])) & STAFF_WRITER_ROLES:
            try:
                pending = db().execute("SELECT count(*) AS c FROM public_report_submission "
                                       "WHERE status='pending'").fetchone()["c"]
            except Exception:  # noqa: BLE001
                pending = None
            items.append({"title": "Review submissions", "href": url_for("intake_review"),
                          "desc": "Triage public/community reports — promote or reject.",
                          "badge": pending or None})
        return render_template("hub.html", hub_title="New Reports",
                               hub_intro="Create a report, or review what's come in from the public and partner apps.",
                               items=items)

    @app.route("/ingest")
    @staff_required
    def ingest_home():
        items = [
            {"title": "Upload CEDEN lab data", "href": url_for("batch_ceden"),
             "desc": "Ingest a CEDEN WaterChemistry CSV, or pull from a URL."},
            {"title": "Ingest lab email folders", "href": url_for("folder_ingest"),
             "desc": "Load a Bend/partner results folder (CSV + CoC/transmittal PDFs); files kept on the batch."},
            {"title": "Lab batch reconciliation", "href": url_for("lab_reconcile"),
             "desc": "Stage a CEDEN chemistry template and link by station + date."},
            {"title": "Sample work area", "href": url_for("samples_list"),
             "desc": "Browse every sample; create one manually or from a CSV; edit location & details."},
            {"title": "Find duplicate samples", "href": url_for("lab_duplicates"),
             "desc": "Detect and merge samples that arrived more than once across ingest paths."},
            {"title": "Lab data workboard", "href": url_for("lab_workboard"),
             "desc": "Assign, link, and QA-review lab samples against reports/cases."},
            {"title": "Bulk sample coordinates", "href": url_for("lab_coordinates"),
             "desc": "Paste station/lat/long rows (e.g. read off chain-of-custody forms) to geocode many samples at once."},
            {"title": "Batch update outcomes", "href": url_for("batch_determination"),
             "desc": "Set the final outcome on many reports at once."},
        ]
        return render_template("hub.html", hub_title="Ingest Data",
                               hub_intro="Bring lab data into the system and connect it to events, reports, and cases.",
                               items=items)

    @app.route("/admin")
    @admin_required
    def admin_home():
        items = [
            {"title": "Accounts", "href": url_for("admin_users"),
             "desc": "Create users and grant or revoke roles."},
            {"title": "Intake groups", "href": url_for("admin_intake_groups"),
             "desc": "Register community/partner groups and mint API keys."},
            {"title": "Analyte taxonomy", "href": url_for("admin_analytes"),
             "desc": "Curate analytes; merge aliases into canonical names."},
            {"title": "Refresh from data.ca.gov", "href": url_for("admin_refresh"),
             "desc": "Pull the latest published reports, cases, responses & results and update the schema."},
            {"title": "Reset / maintenance", "href": url_for("admin_reset"),
             "desc": "Purge lab data to reset the test environment."},
        ]
        return render_template("hub.html", hub_title="Admin",
                               hub_intro="Accounts, partner groups, the analyte vocabulary, and test-environment reset.",
                               items=items)
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
        counties = [r["county"] for r in db().execute(
            "SELECT DISTINCT county FROM waterbody WHERE county IS NOT NULL ORDER BY 1").fetchall()]
        return render_template("map.html", regions=_regions(), counties=counties,
                               determinations=_determinations(),
                               advisories=_recommended_advisories())

    @app.route("/api/reports.geojson")
    @login_required
    def reports_geojson():
        a = request.args
        conn = db()
        try:
            days = int(a["days"]) if a.get("days") else None
        except ValueError:
            days = None

        # "Analytical data without event connections": samples that have results but are not
        # linked to any report/case, plotted at their station location (one marker per station).
        if a.get("data") == "orphan":
            # Geocoded lab samples not yet linked and not tagged routine — the "parked, needs
            # research" set. (Ungeocoded samples have no point and can't appear on a map.)
            cond, p = ["s.bloom_report_id IS NULL", "s.case_id IS NULL", "st.geom IS NOT NULL",
                       "s.sampling_type IS DISTINCT FROM 'routine'"], {}
            if days:
                cond.append("s.sample_date >= current_date - %(days)s::int"); p["days"] = days
            with acting_as(conn, session["uid"]):
                rows = conn.execute(
                    f"""SELECT st.id AS station_id, ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon,
                               st.station_code, st.station_name,
                               count(DISTINCT s.id) AS n_samples,
                               max(s.sample_date)::text AS last_sample
                        FROM sample s JOIN station st ON st.id = s.station_id
                        WHERE {' AND '.join(cond)}
                          AND EXISTS (SELECT 1 FROM result r WHERE r.sample_id = s.id)
                        GROUP BY st.id, st.geom, st.station_code, st.station_name
                        ORDER BY n_samples DESC LIMIT 2000""", p).fetchall()
            features = [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {"kind": "orphan", "station_code": r["station_code"],
                               "station_name": r["station_name"], "n_samples": r["n_samples"],
                               "last_sample": r["last_sample"]},
            } for r in rows]
            return jsonify({"type": "FeatureCollection", "features": features})

        cond, p = [], {}
        if a.get("region"):
            cond.append("w.regional_water_board = %(region)s"); p["region"] = a["region"]
        if a.get("county"):
            cond.append("w.county = %(county)s"); p["county"] = a["county"]
        outcome = a.get("outcome")
        if outcome == "under_investigation":   # the "not recorded" bucket includes NULLs
            cond.append("(e.determination_code = 'under_investigation' OR e.determination_code IS NULL)")
        elif outcome:
            cond.append("e.determination_code = %(outcome)s"); p["outcome"] = outcome
        adv = a.get("advisory")                # filters on the displayed advisory (the lateral)
        if adv == "__any__":
            cond.append("adv.advisory_recommended IS NOT NULL")
        elif adv == "__none__":
            cond.append("adv.advisory_recommended IS NULL")
        elif adv:
            cond.append("adv.advisory_recommended = %(advisory)s"); p["advisory"] = adv
        if days:
            cond.append("e.observation_date >= current_date - %(days)s::int"); p["days"] = days
        if a.get("data") == "with":            # events that have linked analytical results
            cond.append("EXISTS (SELECT 1 FROM sample s JOIN result r ON r.sample_id = s.id "
                        "WHERE s.bloom_report_id = e.bloom_report_id)")
        where = (" WHERE " + " AND ".join(cond)) if cond else ""

        with acting_as(conn, session["uid"]):
            # Kept lean for the free-tier DB: one lateral for the displayed advisory, no
            # per-row count subqueries (the detail page carries the full picture).
            rows = conn.execute(
                f"""SELECT e.bloom_report_id, ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon,
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
                   {where}
                   ORDER BY e.bloom_report_id DESC LIMIT 2000""", p
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

    # ---------- Lab-data reconciliation workboard ----------
    @app.route("/lab/duplicates", methods=["GET", "POST"])
    @staff_required
    def lab_duplicates():
        conn = db()
        if request.method == "POST":
            survivor = (request.form.get("survivor") or "").strip()
            members = request.form.getlist("member")
            if not survivor.isdigit():
                flash("Pick which sample to keep.", "error")
            else:
                try:
                    r = merge_samples(conn, session["uid"], int(survivor), members)
                    flash(f"Merged {r['merged']} duplicate(s) into sample {survivor} — "
                          f"{r['results_repointed']} result(s) moved, {r['results_deduped']} de-duplicated.",
                          "ok" if r["merged"] else "error")
                except (ValueError, psycopg.Error) as exc:
                    conn.rollback()
                    flash("Could not merge: " + str(exc).splitlines()[0], "error")
            return redirect(url_for("lab_duplicates"))
        return render_template("dedup.html", groups=candidate_duplicate_samples(conn))

    # ---------- Sample work area (browse / create / edit sample records) ----------
    @app.route("/lab/samples")
    @staff_required
    def samples_list():
        a = request.args
        f = {k: (a.get(k) or "").strip() or None for k in ("q", "batch", "status", "geocoded")}
        try:
            page = max(0, int(a.get("page", 0)))
        except ValueError:
            page = 0
        per, conn = 100, db()
        rows = list_samples(conn, f, limit=per, offset=page * per)
        total = count_samples(conn, f)
        base_args = {k: v for k, v in a.items() if k != "page"}
        return render_template("samples_list.html", rows=rows, total=total, page=page, per=per,
                               f=f, base_args=base_args)

    @app.route("/lab/samples/new", methods=["GET", "POST"])
    @staff_required
    def sample_new():
        conn = db()
        if request.method == "POST":
            try:
                sid = create_sample(conn, session["uid"], request.form)
                flash("Sample created.", "ok")
                return redirect(url_for("sample_detail", sid=sid))
            except (ValueError, psycopg.Error) as exc:
                conn.rollback()
                flash("Could not create sample: " + str(exc).splitlines()[0], "error")
        return render_template("sample_new.html")

    @app.route("/lab/samples/<int:sid>", methods=["GET", "POST"])
    @staff_required
    def sample_detail(sid):
        conn = db()
        if request.method == "POST":
            try:
                update_sample(conn, session["uid"], sid, request.form)
                flash("Sample updated.", "ok")
            except (ValueError, psycopg.Error) as exc:
                conn.rollback()
                flash("Could not update sample: " + str(exc).splitlines()[0], "error")
            return redirect(url_for("sample_detail", sid=sid))
        data = get_sample(conn, sid)
        if not data:
            flash("Sample not found.", "error")
            return redirect(url_for("samples_list"))
        return render_template("sample_detail.html", **data)

    @app.route("/lab/workboard")
    @staff_required
    def lab_workboard():
        a = request.args
        f = {k: (a.get(k) or "").strip() or None
             for k in ("status", "assignee", "region", "q", "batch", "geocoded", "event")}
        sort = a.get("sort") if a.get("sort") in ("date", "station", "status") else "date"
        try:
            page = max(0, int(a.get("page", 0)))
        except ValueError:
            page = 0
        per, conn = 100, db()
        rows = workboard(conn, f, me=session["uid"], sort=sort, limit=per, offset=page * per)
        total = count_workboard(conn, f, me=session["uid"])
        base_args = {k: v for k, v in a.items() if k != "page"}
        # When scoped to one ingest batch, surface the batch header + its source files.
        batch = files = None
        if str(f.get("batch") or "").isdigit():
            batch = conn.execute("SELECT * FROM lab_batch WHERE id=%s", (int(f["batch"]),)).fetchone()
            files = batch_files(conn, int(f["batch"])) if batch else None
        return render_template("workboard.html", rows=rows, total=total, page=page, per=per, f=f,
                               sort=sort, tallies=status_tallies(conn), team=team_members(conn),
                               regions=_regions(), base_args=base_args, batch=batch, batch_files=files)

    @app.route("/lab/workboard/reconcile", methods=["POST"])
    @staff_required
    def lab_workboard_reconcile():
        f = request.form
        try:
            days = max(1, int(f.get("days") or 14))
        except ValueError:
            days = 14
        ids = [int(x) for x in f.getlist("sample_ids") if x.isdigit()]
        if not ids:  # no checked rows -> reconcile every UNLINKED sample matching the current filter
            filt = {k: (f.get(k) or "").strip() or None for k in ("assignee", "region", "q", "event", "geocoded")}
            filt["status"] = "unlinked"
            ids = [r["id"] for r in workboard(db(), filt, me=session["uid"], limit=5000)]
        res = batch_reconcile_samples(db(), ids, days=days)
        flash(f"Batch reconcile (±{days} d): linked {res['linked']}, "
              f"skipped {res['skipped']} (no confident match — review manually).", "ok")
        return redirect(request.referrer or url_for("lab_workboard"))

    @app.route("/lab/workboard/assign", methods=["POST"])
    @staff_required
    def lab_workboard_assign():
        f = request.form
        ids = [int(x) for x in f.getlist("sample_ids") if x.isdigit()]
        who = f.get("assignee_id")
        assignee = int(who) if who and who.isdigit() else None
        n = assign_samples(db(), session["uid"], ids, assignee)
        flash(f"{'Assigned' if assignee else 'Unassigned'} {n} sample(s).", "ok")
        return redirect(request.referrer or url_for("lab_workboard"))

    @app.route("/lab/sample/<int:sid>/link", methods=["POST"])
    @staff_required
    def lab_sample_link(sid):
        conn, f = db(), request.form
        ev = (f.get("bloom_report_id") or "").strip()
        case = (f.get("case_id") or "").strip()
        if not ev.isdigit() and not case.isdigit():
            flash("Enter a report ID or case ID to link to.", "error")
            return redirect(request.referrer or url_for("lab_workboard"))
        try:
            link_sample(conn, session["uid"], sid,
                        bloom_report_id=int(ev) if ev.isdigit() else None,
                        case_id=int(case) if case.isdigit() else None)
            flash("Sample linked — pending QA review.", "ok")
        except psycopg.errors.ForeignKeyViolation:
            conn.rollback(); flash("No such report/case ID.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not link: " + str(exc).splitlines()[0], "error")
        return redirect(request.referrer or url_for("lab_workboard"))

    @app.route("/lab/sample/<int:sid>/link-selected", methods=["POST"])
    @staff_required
    def lab_sample_link_selected(sid):
        conn = db()
        try:
            res = link_sample_to_reports(conn, session["uid"], sid,
                                         request.form.getlist("report_ids"))
        except psycopg.Error as exc:
            conn.rollback()
            flash("Could not link: " + str(exc).splitlines()[0], "error")
            return redirect(request.referrer or url_for("lab_workboard"))
        if res.get("error"):
            flash(res["error"], "error")
        elif res["linked"] == "case":
            flash(f"Linked to Case {res['id']} — covers {len(res['reports'])} selected report(s). "
                  "Pending QA review.", "ok")
        else:
            flash(f"Sample linked to R{res['id']} — pending QA review.", "ok")
        return redirect(request.referrer or url_for("lab_workboard"))

    @app.route("/lab/sample/<int:sid>/link-stations", methods=["POST"])
    @staff_required
    def lab_sample_link_stations(sid):
        conn = db()
        codes = [c for c in request.form.getlist("station_code") if c.strip()]
        try:
            n = link_sample_stations(conn, session["uid"], sid, codes)
            flash(f"Linked {n} CEDEN station location(s) to the sample." if n
                  else "Select a CEDEN station to link.", "ok" if n else "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not link station(s): " + str(exc).splitlines()[0], "error")
        return redirect(request.referrer or url_for("lab_workboard"))

    @app.route("/lab/sample/<int:sid>/unlink-station", methods=["POST"])
    @staff_required
    def lab_sample_unlink_station(sid):
        unlink_sample_station(db(), session["uid"], sid, (request.form.get("station_code") or "").strip())
        flash("CEDEN station location unlinked.", "ok")
        return redirect(request.referrer or url_for("lab_workboard"))

    @app.route("/lab/sample/<int:sid>/unlink", methods=["POST"])
    @staff_required
    def lab_sample_unlink(sid):
        unlink_sample(db(), session["uid"], sid)
        flash("Sample unlinked.", "ok")
        return redirect(request.referrer or url_for("lab_workboard"))

    @app.route("/lab/sample/<int:sid>/create-report", methods=["POST"])
    @staff_required
    def lab_sample_create_report(sid):
        conn = db()
        try:
            at = _coords_arg(request.form)   # optional: geocode the sample first (from the map)
            if at:
                set_sample_location(conn, sid, at[0], at[1])
            brid = create_report_from_sample(conn, session["uid"], sid,
                                             region=(request.form.get("region") or "").strip() or None)
            _record_activity(brid, "created report from lab sample")
            flash(f"Created report {brid} and linked the sample.", "ok")
        except (psycopg.Error, ValueError) as exc:
            conn.rollback(); flash("Could not create report: " + str(exc).splitlines()[0], "error")
        return redirect(request.referrer or url_for("lab_workboard"))

    @app.route("/lab/sample/<int:sid>/routine", methods=["POST"])
    @staff_required
    def lab_sample_routine(sid):
        conn = db()
        if request.form.get("undo"):
            clear_routine(conn, session["uid"], sid)
            flash("Sample returned to the unlinked queue.", "ok")
            return redirect(request.referrer or url_for("lab_workboard"))
        try:
            at = _coords_arg(request.form)   # optional: also geocode it (from the map)
            if at:
                set_sample_location(conn, sid, at[0], at[1])
            tag_routine(conn, session["uid"], sid,
                        subtype=(request.form.get("subtype") or "").strip() or None)
            flash("Tagged as routine sampling.", "ok")
        except (psycopg.Error, ValueError) as exc:
            conn.rollback(); flash("Could not tag sample: " + str(exc).splitlines()[0], "error")
        return redirect(request.referrer or url_for("lab_workboard"))

    def _coords_arg(src):
        """Parse lat/lon from a request source; returns (lat, lon) floats or None."""
        try:
            lat, lon = src.get("lat"), src.get("lon")
            if lat in (None, "") or lon in (None, ""):
                return None
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None

    @app.route("/lab/sample/<int:sid>/geo.json")
    @staff_required
    def lab_sample_geo(sid):
        return jsonify(sample_geo(db(), sid, at=_coords_arg(request.args)))

    @app.route("/lab/sample/<int:sid>/geocode", methods=["POST"])
    @staff_required
    def lab_sample_geocode(sid):
        conn = db()
        at = _coords_arg(request.form)
        if not at:
            return jsonify({"error": "Enter a valid latitude and longitude."}), 400
        try:
            set_sample_location(conn, sid, at[0], at[1])
        except ValueError as exc:
            conn.rollback()
            return jsonify({"error": str(exc)}), 400
        return jsonify(sample_geo(conn, sid))

    @app.route("/lab/sample/<int:sid>/ocr-coords")
    @staff_required
    def lab_sample_ocr_coords(sid):
        conn = db()
        coc = conn.execute(
            """SELECT f.filename, f.data FROM lab_batch_file f
               JOIN sample s ON s.lab_batch_id = f.batch_id
               WHERE s.id = %s AND f.category = 'coc' ORDER BY f.id LIMIT 1""", (sid,)).fetchone()
        if not coc:
            return jsonify({"error": "No chain-of-custody file stored for this sample's batch."}), 404
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        try:
            tmp.write(bytes(coc["data"])); tmp.close()
            found = ocr_pdf_coords(tmp.name)
        except OcrUnavailable as exc:
            return jsonify({"error": "OCR not available in this environment. "
                                     "Read the coordinates off the CoC and type them in. "
                                     f"({str(exc).splitlines()[0]})", "file": coc["filename"]}), 503
        finally:
            os.unlink(tmp.name)
        if not found:
            return jsonify({"error": "No coordinates recognized on the CoC. Enter them manually.",
                            "file": coc["filename"]}), 200
        return jsonify({**found, "file": coc["filename"]})

    @app.route("/lab/sample/<int:sid>/qa", methods=["POST"])
    @staff_required
    def lab_sample_qa(sid):
        f = request.form
        qa_review(db(), session["uid"], sid, approve=(f.get("action") == "approve"),
                  note=(f.get("note") or "").strip() or None)
        flash("QA " + ("approved." if f.get("action") == "approve" else "flagged for rework."), "ok")
        return redirect(request.referrer or url_for("lab_workboard"))

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

    # ---------- Analyte taxonomy admin ----------
    @app.route("/admin/analytes")
    @admin_required
    def admin_analytes():
        return render_template("analytes.html", analytes=list_analytes(db()),
                               analysis_types=["Cyanotoxin", "Field Measurement", "Genetic",
                                               "Microscopy", "Pigment", "Other"])

    @app.route("/admin/analytes/<int:aid>/edit", methods=["POST"])
    @admin_required
    def admin_analyte_edit(aid):
        f = request.form
        try:
            update_analyte(db(), aid,
                           analysis_type=(f.get("analysis_type") or "").strip() or None,
                           analyte_class=(f.get("analyte_class") or "").strip() or None,
                           analyte=(f.get("analyte") or "").strip() or None,
                           default_unit=(f.get("default_unit") or "").strip() or None)
            flash("Analyte updated.", "ok")
        except TaxonomyError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_analytes"))

    @app.route("/admin/analytes/<int:aid>/merge", methods=["POST"])
    @admin_required
    def admin_analyte_merge(aid):
        target = request.form.get("target_id")
        try:
            moved = merge_analytes(db(), aid, int(target) if target else 0)
            flash(f"Merged — moved {moved} result(s) to the canonical analyte.", "ok")
        except TaxonomyError as exc:
            flash(str(exc), "error")
        except (ValueError, psycopg.Error) as exc:
            db().rollback(); flash("Could not merge: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("admin_analytes"))

    @app.route("/admin/analytes/<int:aid>/delete", methods=["POST"])
    @admin_required
    def admin_analyte_delete(aid):
        try:
            delete_analyte(db(), aid)
            flash("Analyte deleted.", "ok")
        except TaxonomyError as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin_analytes"))

    # ---------- Refresh from data.ca.gov (pull latest published records) ----------
    @app.route("/admin/refresh", methods=["GET", "POST"])
    @admin_required
    def admin_refresh():
        report = mode = None
        if request.method == "POST":
            apply = request.form.get("apply") == "1"
            if apply and request.form.get("confirm") != "UPDATE":
                flash('Type UPDATE to confirm applying the refresh.', "error")
            else:
                try:
                    rep = refresh_from_ca_gov(db(), dry_run=not apply)
                    report = {"inserted": rep.inserted, "updated": rep.updated,
                              "skipped": rep.skipped}
                    mode = "apply" if apply else "preview"
                    if apply:
                        tot_i = sum(rep.inserted.values()); tot_u = sum(rep.updated.values())
                        flash(f"Applied — inserted {tot_i}, updated {tot_u} record(s).", "ok")
                except RefreshError as exc:
                    flash("Could not reach data.ca.gov: " + str(exc).splitlines()[0], "error")
                except Exception as exc:  # noqa: BLE001
                    db().rollback()
                    flash("Refresh failed: " + str(exc).splitlines()[0], "error")
        return render_template("admin_refresh.html", report=report, mode=mode,
                               dataset_url=DATASET_URL)

    # ---------- Reset / maintenance (test environment) ----------
    @app.route("/admin/reset")
    @admin_required
    def admin_reset():
        counts = lab_data_counts(db())
        return render_template("admin_reset.html", counts=counts,
                               lab_tables=LAB_TABLES, kept_tables=KEPT_TABLES,
                               lab_total=sum(counts[t] for t in LAB_TABLES))

    @app.route("/admin/reset/purge-lab", methods=["POST"])
    @admin_required
    def admin_reset_purge_lab():
        if (request.form.get("confirm") or "").strip().upper() != "RESET":
            flash("Type RESET to confirm — nothing was deleted.", "error")
            return redirect(url_for("admin_reset"))
        conn = db()
        try:
            deleted = purge_lab_data(conn)
            n = sum(deleted.values())
            flash(f"Lab data purged — deleted {n:,} row(s) "
                  f"({deleted.get('sample', 0):,} samples, {deleted.get('result', 0):,} results).", "ok")
        except psycopg.Error as exc:
            conn.rollback()
            flash("Purge failed: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("admin_reset"))

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
                   FROM lab_batch b WHERE b.kind='staged' ORDER BY b.id DESC LIMIT 50""").fetchall()
        return render_template("lab_reconcile.html", batches=batches)

    @app.route("/ingest/folders", methods=["GET", "POST"])
    @staff_required
    def folder_ingest():
        conn = db()
        if request.method == "POST":
            # `ajax` is set by the multi-folder uploader, which POSTs one subfolder at a time
            # and wants JSON back (no redirect) so it can show progress across many folders.
            ajax = request.form.get("ajax")
            uploads = [f for f in request.files.getlist("files") if f and f.filename]
            source = (request.form.get("source") or "").strip()
            if not uploads:
                if ajax:
                    return jsonify({"error": "no files"}), 400
                flash("Choose the files from one lab email folder (results CSV + any PDFs).", "error")
                return redirect(url_for("folder_ingest"))
            tmpdir = tempfile.mkdtemp()
            try:
                for up in uploads:
                    up.save(os.path.join(tmpdir, os.path.basename(up.filename)))
                r = ingest_bend_folder(conn, tmpdir, source=source or None)
                if ajax:
                    return jsonify(r)
                flash(f"Ingested {r['samples']} sample(s) ({r['geocoded']} geocoded), "
                      f"{r['results']} result(s); stored {r['files']} file(s).", "ok")
                return redirect(url_for("lab_workboard", batch=r["batch_id"], status="unlinked"))
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                if ajax:
                    return jsonify({"error": str(exc).splitlines()[0], "source": source}), 400
                flash("Could not ingest folder: " + str(exc).splitlines()[0], "error")
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        batches = ingested_batches(conn)
        for b in batches:
            b["files"] = batch_files(conn, b["id"])
        return render_template("folder_ingest.html", batches=batches)

    @app.route("/lab/batch/<int:bid>/coordinates", methods=["GET", "POST"])
    @staff_required
    def batch_coordinates(bid):
        conn = db()
        batch = conn.execute("SELECT * FROM lab_batch WHERE id=%s", (bid,)).fetchone()
        if not batch:
            flash("Batch not found.", "error")
            return redirect(url_for("folder_ingest"))
        if request.method == "POST":
            applied = 0
            try:
                for sid in request.form.getlist("sample_id"):
                    lat = (request.form.get(f"lat_{sid}") or "").strip()
                    lon = (request.form.get(f"lon_{sid}") or "").strip()
                    if lat and lon:
                        set_sample_point(conn, int(sid), lat, lon)
                        applied += 1
                conn.commit()
                flash(f"Saved coordinates for {applied} sample(s).", "ok")
            except (ValueError, psycopg.Error) as exc:
                conn.rollback()
                flash("Could not save coordinates: " + str(exc).splitlines()[0], "error")
            return redirect(url_for("batch_coordinates", bid=bid))
        samples = conn.execute(
            """SELECT s.id, st.station_code, st.station_name, s.sample_id, s.bg_id,
                      s.lab_sample_id, s.sample_type, s.sample_date::text AS sample_date,
                      ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon
               FROM sample s LEFT JOIN station st ON st.id = s.station_id
               WHERE s.lab_batch_id = %s ORDER BY s.id""", (bid,)).fetchall()
        return render_template("batch_coordinates.html", batch=batch, samples=samples,
                               files=batch_files(conn, bid))

    @app.route("/lab/coordinates", methods=["GET", "POST"])
    @staff_required
    def lab_coordinates():
        conn, result = db(), None
        text = request.form.get("rows", "") if request.method == "POST" else ""
        if request.method == "POST":
            try:
                result = bulk_geocode(conn, session["uid"], text)
                if result["applied"]:
                    flash(f"Geocoded {result['applied']} station/sample entr(ies), "
                          f"{result['samples']} sample(s).", "ok")
                else:
                    flash("No rows matched a station or sample.", "error")
            except Exception as exc:  # noqa: BLE001
                conn.rollback(); flash("Could not apply coordinates: " + str(exc).splitlines()[0], "error")
        return render_template("bulk_coordinates.html", result=result, text=text)

    @app.route("/batch/<int:bid>/file/<int:fid>")
    @staff_required
    def batch_file_download(bid, fid):
        conn = db()
        f = batch_file(conn, fid)
        if not f:
            flash("File not found.", "error")
            return redirect(url_for("folder_ingest"))
        return Response(bytes(f["data"]),
                        mimetype=f["content_type"] or "application/octet-stream",
                        headers={"Content-Disposition":
                                 f'inline; filename="{f["filename"]}"'})

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
