"""Staff web app: role-based account management and report data entry (Flask).

Data operations run through the database's Row-Level Security as the logged-in user
(`fhab.auth.acting_as`); account management runs with the privileged connection but is gated
to `program_admin` at the app layer. See docs/USER_ROLES.md.
"""

from __future__ import annotations

import os
from functools import wraps

import psycopg
from flask import (Flask, flash, g, redirect, render_template, request, session, url_for)

from ..auth import (acting_as, authenticate, create_user, grant_role, list_roles_for,
                    revoke_role, set_password, user_regions)
from ..db import DEFAULT_DSN, connect
from ..reports import enter_report


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
