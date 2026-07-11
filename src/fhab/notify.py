"""In-app notifications, routed to staff by role, with escalation for suspected illness.

Notifications are created on the privileged connection (system events have no logged-in user)
and read by their owner under RLS. Email is an optional seam: if FHAB_SMTP_HOST is configured,
escalations are also emailed; otherwise it's in-app only (fine for the testing phase).
"""

from __future__ import annotations

import os

import psycopg

from .auth import acting_as

# Who is notified for which event.
REVIEWER_ROLES = ["program_admin", "wb_staff"]            # new submissions to triage
ILLNESS_ROLES = ["program_admin", "illness_workgroup"]    # suspected illness escalation


def send_email(to: str, subject: str, body: str | None) -> bool:
    """Best-effort email via SMTP if configured (env). Returns True on send, else False."""
    host = os.environ.get("FHAB_SMTP_HOST")
    if not host or not to:
        return False
    import smtplib
    import ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = os.environ.get("FHAB_SMTP_FROM", "fhab@no-reply.ca.gov")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body or "")
    try:
        with smtplib.SMTP(host, int(os.environ.get("FHAB_SMTP_PORT", "587"))) as s:
            s.starttls(context=ssl.create_default_context())
            user, pw = os.environ.get("FHAB_SMTP_USER"), os.environ.get("FHAB_SMTP_PASS")
            if user:
                s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception:  # noqa: BLE001 - never let mail failure break the request
        return False


def users_with_roles(conn: psycopg.Connection, role_codes) -> list:
    """Active users holding any of the given roles (privileged read). Returns [{id, email}]."""
    return conn.execute(
        """SELECT DISTINCT u.id, u.email FROM app_user u
           JOIN user_role ur ON ur.user_id = u.id
           WHERE ur.role_code = ANY(%s) AND u.is_active""", (list(role_codes),)).fetchall()


def notify_users(conn: psycopg.Connection, recipients, *, kind, title, body=None, link=None,
                 submission_id=None, bloom_report_id=None, email=False) -> int:
    """Create one notification per recipient (privileged insert). Optionally email each."""
    n = 0
    for u in recipients:
        conn.execute(
            """INSERT INTO notification
                 (user_id, kind, title, body, link, submission_id, bloom_report_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (u["id"], kind, title, body, link, submission_id, bloom_report_id))
        if email and u.get("email"):
            send_email(u["email"], title, body)
        n += 1
    conn.commit()
    return n


def on_new_submission(conn: psycopg.Connection, submission_id: int, *, water_body=None,
                      has_illness=False, source=None) -> None:
    """Notify reviewers of a new public submission; escalate to the illness workgroup if needed.

    If an admin has enabled email forwarding of new-report notices, the reviewer notifications are
    also emailed and a copy is sent to the configured forward-to address (requires SMTP configured).
    """
    from .settings import FORWARD_TO, email_new_report_enabled, get_setting
    wb = water_body or "a waterbody"
    src = f" (via {source})" if source else ""
    email_on = email_new_report_enabled(conn)
    title = f"New bloom report: {wb}"
    body = f"A public submission for {wb}{src} is awaiting review."
    notify_users(
        conn, users_with_roles(conn, REVIEWER_ROLES), kind="new_submission",
        title=title, body=body, link="/intake/review", submission_id=submission_id, email=email_on)
    forward_to = get_setting(conn, FORWARD_TO)
    if email_on and forward_to:
        send_email(forward_to, title, body + "\n\nReview: /intake/review")
    if has_illness:
        notify_users(
            conn, users_with_roles(conn, ILLNESS_ROLES), kind="illness_alert",
            title=f"⚠ Suspected illness reported: {wb}",
            body=f"A submission for {wb} reports suspected human/animal illness or death. "
                 f"Please review promptly.",
            link="/intake/review", submission_id=submission_id, email=True)


def list_notifications(conn: psycopg.Connection, user_id: int, limit: int = 50) -> list:
    with acting_as(conn, user_id):
        return conn.execute(
            """SELECT id, kind, title, body, link, read_at, created_at
               FROM notification WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
            (user_id, limit)).fetchall()


def unread_count(conn: psycopg.Connection, user_id: int) -> int:
    with acting_as(conn, user_id):
        return conn.execute(
            "SELECT count(*) AS c FROM notification WHERE user_id = %s AND read_at IS NULL",
            (user_id,)).fetchone()["c"]


def mark_read(conn: psycopg.Connection, user_id: int, nid: int | None = None) -> None:
    """Mark one notification (nid) or all of the user's notifications as read."""
    with acting_as(conn, user_id):
        if nid is not None:
            conn.execute(
                "UPDATE notification SET read_at = now() WHERE id = %s AND user_id = %s AND read_at IS NULL",
                (nid, user_id))
        else:
            conn.execute(
                "UPDATE notification SET read_at = now() WHERE user_id = %s AND read_at IS NULL",
                (user_id,))
        conn.commit()
