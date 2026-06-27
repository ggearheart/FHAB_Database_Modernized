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

from ..auth import (acting_as, authenticate, create_user, grant_role, list_roles_for,
                    revoke_role, set_password, user_regions)
from ..db import DEFAULT_DSN, connect
from ..reports import add_result, enter_report, update_report


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

    DATA_TYPES = ["Field Visual", "Field Measurement", "Laboratory"]

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
        return render_template("dashboard.html", regions=user_regions(db(), session["uid"]))

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
        return render_template("report_detail.html", ev=ev, results=results, responses=responses,
                               case=case, determinations=_determinations(), analytes=_analytes(),
                               data_types=DATA_TYPES)

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
            return redirect(url_for("batch_determination", case=request.form.get("case") or None))

        case_filter = (request.args.get("case") or "").strip()
        conn = db()
        with acting_as(conn, session["uid"]):
            if case_filter.isdigit():
                rows = conn.execute(
                    """SELECT e.bloom_report_id, w.water_body_name, w.regional_water_board,
                              e.determination_code, e.case_id
                       FROM event e LEFT JOIN location l ON l.id = e.location_id
                       LEFT JOIN waterbody w ON w.id = l.waterbody_id
                       WHERE e.case_id = %s ORDER BY e.bloom_report_id""", (int(case_filter),)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT e.bloom_report_id, w.water_body_name, w.regional_water_board,
                              e.determination_code, e.case_id
                       FROM event e LEFT JOIN location l ON l.id = e.location_id
                       LEFT JOIN waterbody w ON w.id = l.waterbody_id
                       ORDER BY e.bloom_report_id DESC LIMIT 200""").fetchall()
        return render_template("batch.html", rows=rows, determinations=_determinations(),
                               case_filter=case_filter)

    @app.route("/map")
    @login_required
    def report_map():
        return render_template("map.html")

    @app.route("/api/reports.geojson")
    @login_required
    def reports_geojson():
        conn = db()
        with acting_as(conn, session["uid"]):
            rows = conn.execute(
                """SELECT e.bloom_report_id, ST_Y(l.geom) AS lat, ST_X(l.geom) AS lon,
                          w.water_body_name, w.regional_water_board, e.observation_date::text AS obs,
                          e.event_status, e.determination_code, rd.label AS det_label, e.case_id,
                          (SELECT count(*) FROM response r WHERE r.bloom_report_id = e.bloom_report_id) AS responses,
                          (SELECT count(*) FROM sample s JOIN result rs ON rs.sample_id = s.id
                             WHERE s.bloom_report_id = e.bloom_report_id) AS results,
                          (SELECT a.advisory_recommended FROM response r
                             JOIN advisory a ON a.response_action_id = r.response_action_id
                             WHERE r.bloom_report_id = e.bloom_report_id AND a.display_advisory_on_map
                             ORDER BY a.advisory_start_date DESC NULLS LAST LIMIT 1) AS advisory
                   FROM event e
                   JOIN location l ON l.id = e.location_id
                   LEFT JOIN waterbody w ON w.id = l.waterbody_id
                   LEFT JOIN report_determination rd ON rd.code = e.determination_code
                   WHERE l.geom IS NOT NULL
                   ORDER BY e.bloom_report_id DESC LIMIT 2000"""
            ).fetchall()
        props = ("bloom_report_id", "water_body_name", "regional_water_board", "obs",
                 "event_status", "determination_code", "det_label", "case_id",
                 "responses", "results", "advisory")
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
            flash("Result added.", "ok")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); flash("Access denied: you may not add results to that report.", "error")
        except psycopg.Error as exc:
            conn.rollback(); flash("Could not add result: " + str(exc).splitlines()[0], "error")
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
