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
from ..db import DEFAULT_DSN, connect
from ..geo import GEOCONNEX
from ..reports import add_response, add_result, enter_report, update_report


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

    # ---- routes ----
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
                          e.bloom_texture, e.surface_water_condition, e.weather_condition,
                          e.bloom_description, e.case_id,
                          w.water_body_name, w.regional_water_board, w.county
                   FROM event e
                   LEFT JOIN location l ON l.id = e.location_id
                   LEFT JOIN waterbody w ON w.id = l.waterbody_id
                   WHERE e.bloom_report_id = %s""", (brid,)).fetchone()
            if not ev:
                flash("Report not found or not visible to your role.", "error")
                return redirect(url_for("reports"))
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
                               case=case, locations=locations,
                               determinations=_determinations(), analytes=_analytes(),
                               data_types=DATA_TYPES, recommended_advisories=_recommended_advisories(),
                               response_categories=RESPONSE_CATEGORIES)

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
                surface_water_condition=(f.get("surface_water_condition") or "").strip() or None,
                weather_condition=(f.get("weather_condition") or "").strip() or None,
                bloom_description=(f.get("bloom_description") or "").strip() or None,
                determination=(f.get("determination_code") or "").strip() or None,
            )
            _record_activity(brid, "edited report")
            flash("Report updated.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: you may not edit that report.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not update: " + str(exc).splitlines()[0], "error")
        return redirect(url_for("report_detail", brid=brid))

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
                                       determinations=_determinations(), cross_warn=(regs, region))
            try:
                rid = enter_report(
                    conn, session["uid"],
                    water_body_name=f["waterbody"].strip(), region=region,
                    county=(f.get("county") or "").strip() or None,
                    lat=_f(f.get("lat")), lon=_f(f.get("lon")),
                    observation_date=(f.get("date") or "").strip() or None,
                    report_type=(f.get("report_type") or "Staff entry").strip(),
                    bloom_type=(f.get("bloom_type") or "").strip() or None,
                    bloom_size=(f.get("bloom_size") or "").strip() or None,
                    description=(f.get("description") or "").strip() or None,
                    determination=(f.get("determination_code") or "").strip() or None,
                )
                _record_activity(rid, "entered report")
                flash(f"Report entered — Bloom_Report_ID {rid}.", "ok")
                return redirect(url_for("reports"))
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                flash("Access denied: your role may not file this report.", "error")
            except psycopg.Error as exc:
                conn.rollback()
                flash("Could not enter report: " + str(exc).splitlines()[0], "error")
        return render_template("new_report.html", form={}, regions=_regions(),
                               determinations=_determinations(), cross_warn=None)

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
