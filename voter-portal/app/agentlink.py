"""Private read-only dashboard links for agents (no sign-in needed).

The Administrator makes a link for one agent and sends it on WhatsApp. Whoever opens the link sees
that agent's voters only (photo, name, house, phone, latest status): no editing, no export, nothing
from the confirmed list. Links expire, can be stopped or replaced at any time, and every open is
counted so a forwarded link shows up on the agent's card.
"""
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   send_from_directory, url_for)

from . import audit, campaign
from .auth import can_manage_confirmed_voters
from .db import get_db, now_iso

bp = Blueprint("agentlink", __name__)

DAYS = (7, 30, 90)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_links (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_by INTEGER,
    expires_at TEXT NOT NULL,
    stopped_at TEXT,
    opens INTEGER NOT NULL DEFAULT 0,
    last_open_at TEXT
)
"""


def init_schema(conn):
    conn.execute(SCHEMA)
    conn.commit()


def _now():
    return datetime.now(timezone.utc)


def _local_day(stamp):
    return (datetime.fromisoformat(stamp) + timedelta(hours=5)).strftime("%-d %b")


def active_links():
    """{agent id: their live link row} for the Agents page."""
    rows = get_db().execute("SELECT * FROM agent_links WHERE stopped_at IS NULL AND expires_at > ? ORDER BY id",
                            (_now().isoformat(timespec="seconds"),)).fetchall()
    return {r["user_id"]: r for r in rows}


def link_url(row):
    return url_for("agentlink.view", token=row["token"], _external=True)


def whatsapp_url(row, agent):
    from .share import _wa_number

    text = (f"*MDP Gadhdhoo · Your voter list*\n{link_url(row)}\n\n"
            f"Live and read-only, valid until {_local_day(row['expires_at'])}. Please do not forward this link.")
    number = _wa_number(agent["phone"]) or ""
    return f"https://wa.me/{number}?text={quote(text)}"


def _agent(agent_id):
    row = get_db().execute("SELECT * FROM users WHERE id = ? AND deleted_at IS NULL AND role IN ('agent', 'user', 'admin')",
                           (agent_id,)).fetchone()
    return row or abort(404)


def _stop_all(db, agent_id):
    db.execute("UPDATE agent_links SET stopped_at = ? WHERE user_id = ? AND stopped_at IS NULL", (now_iso(), agent_id))


@bp.post("/admin/agents/<int:agent_id>/link")
@can_manage_confirmed_voters
def create(agent_id):
    agent = _agent(agent_id)
    days = request.form.get("days", type=int)
    days = days if days in DAYS else DAYS[0]
    db = get_db()
    _stop_all(db, agent_id)  # one live link per agent: a new link replaces the old one
    db.execute("INSERT INTO agent_links (user_id, token, created_at, created_by, expires_at) VALUES (?, ?, ?, ?, ?)",
               (agent_id, secrets.token_urlsafe(24), now_iso(), g.user["id"],
                (_now() + timedelta(days=days)).isoformat(timespec="seconds")))
    audit.log("agent_link", target=agent["username"], detail={"days": days}, commit=False)
    db.commit()
    flash(f"Dashboard link for {agent['username']} is ready. Tap “Send link on WhatsApp”.", "success")
    return redirect(url_for("canvass.agents") + f"#agent-{agent_id}")


@bp.post("/admin/agents/<int:agent_id>/link/stop")
@can_manage_confirmed_voters
def stop(agent_id):
    agent = _agent(agent_id)
    db = get_db()
    _stop_all(db, agent_id)
    audit.log("agent_link_stop", target=agent["username"], commit=False)
    db.commit()
    flash(f"{agent['username']}'s dashboard link no longer works.", "success")
    return redirect(url_for("canvass.agents") + f"#agent-{agent_id}")


def _live(token):
    if not token or len(token) > 64:
        abort(404)
    row = get_db().execute("SELECT * FROM agent_links WHERE token = ?", (token,)).fetchone()
    if row is None:
        abort(404)
    if row["stopped_at"] or row["expires_at"] <= _now().isoformat(timespec="seconds"):
        return row, False
    return row, True


def _voters(agent_id):
    from .voters import handled_by

    where = handled_by()
    params = [agent_id, agent_id]
    clause, extra = campaign.campaign_clause()
    if clause:
        where += f" AND {clause}"
        params += extra
    return get_db().execute(
        f"SELECT v.national_id, v.full_name, v.full_name_dv, v.house_name, v.house_name_dv, v.phone, v.photo_path,"
        f" v.status, v.location, v.deceased, k.status AS contact, k.at AS contact_at FROM voters v"
        f" LEFT JOIN war_contact k ON k.national_id = v.national_id"
        f" WHERE v.national_id IN (SELECT national_id FROM voters WHERE {where})"
        " ORDER BY v.house_name = '', v.house_name COLLATE NOCASE, v.full_name COLLATE NOCASE", params).fetchall()


@bp.route("/a/<token>")
def view(token):
    from .voters import STATUSES

    link, live = _live(token)
    if not live:
        return render_template("agentlink/ended.html"), 410
    db = get_db()
    agent = db.execute("SELECT id, username FROM users WHERE id = ? AND deleted_at IS NULL", (link["user_id"],)).fetchone()
    if agent is None:
        return render_template("agentlink/ended.html"), 410
    db.execute("UPDATE agent_links SET opens = opens + 1, last_open_at = ? WHERE id = ?", (now_iso(), link["id"]))
    db.commit()
    rows = _voters(agent["id"])
    houses = {}
    for r in rows:
        houses.setdefault(r["house_name"] or "", []).append(r)
    from .warroom import CONTACT, _hm

    counts = {k: sum(1 for r in rows if r["contact"] == k) for k in CONTACT}
    t = {"total": len(rows), "reached": sum(counts.values()), "counts": counts}
    resp = current_app.make_response(render_template(
        "agentlink/view.html", agent=agent, houses=houses, t=t, token=token, statuses=STATUSES, contact=CONTACT,
        hm=_hm, until=_local_day(link["expires_at"])))
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"  # the link never leaks to other sites
    return resp


@bp.post("/a/<token>/contact")
def contact(token):
    """The agent taps what a voter said (Coming, Needs transport, Call later, No answer, Not coming; '' clears).
    Only for the agent's own voters, while the link is live. Same statuses as the War Room."""
    from flask import jsonify

    from .voters import handled_by
    from .warroom import CONTACT, request_transport, set_contact

    link, live = _live(token)
    if not live:
        abort(410)
    db = get_db()
    agent = db.execute("SELECT id, username FROM users WHERE id = ? AND deleted_at IS NULL", (link["user_id"],)).fetchone()
    nid = request.form.get("national_id", "")
    status = request.form.get("status", "")
    if agent is None or (status and status not in CONTACT):
        abort(400)
    if not db.execute(f"SELECT 1 FROM voters WHERE national_id = ? AND {handled_by()}", (nid, agent["id"], agent["id"])).fetchone():
        abort(403)
    if status:
        set_contact(nid, status, by=agent["id"])
        if status == "transport":
            request_transport(nid, "agent link", by=agent["id"])
    else:
        db.execute("DELETE FROM war_contact WHERE national_id = ?", (nid,))
    audit.log("agent_link_contact", target=nid, detail={"agent": agent["username"], "status": status}, user=agent,
              commit=False)
    db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "status": status, "label": CONTACT.get(status, "Not reached yet")})
    return redirect(url_for("agentlink.view", token=token))


@bp.route("/a/<token>/p/<national_id>")
def photo(token, national_id):
    from .voters import handled_by

    link, live = _live(token)
    if not live:
        abort(404)
    voter = get_db().execute(f"SELECT photo_path FROM voters WHERE national_id = ? AND {handled_by()}",
                             (national_id, link["user_id"], link["user_id"])).fetchone()
    if voter is None or not voter["photo_path"]:
        abort(404)
    resp = send_from_directory(current_app.config["PHOTOS_DIR"], voter["photo_path"], max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp
