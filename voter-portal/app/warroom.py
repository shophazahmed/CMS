"""Election-day War Room.

Tier 1 (box agents) mark voters "Voted" as the official calls their number from the official list.
Tier 2 (field runners) see their houses' supporters who haven't voted yet and can mark them too.
Tier 3 (call centre) phone the supporters on their call list; a voter clears from the list as soon as
anyone marks them voted. The War Room dashboard shows turnout, our supporters voted / pending per box,
the call centre and the agents, live.

Built for poor mobile signal: pages are light, phones ask for changes every few seconds (only the IDs
that changed come back), and marks made without signal are kept on the phone and sent when it returns
(each mark has a key, so a resend never counts twice). Field accounts (tiers 1-3) see only the War Room
pages and only their own voters: no search, no lists of everyone, no downloads; lookups are by exact
number and rate-limited. The owner gives each account its tier, opens and closes the War Room, and
can import the official list numbers.
"""
import io
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from . import audit, campaign, pledges
from .auth import is_owner, login_required
from .db import get_db, now_iso
from .normalize import clean_text, normalize_id

bp = Blueprint("warroom", __name__)

SCHEMA = """
-- The official list: each voter's number on their box's list (imported).
CREATE TABLE IF NOT EXISTS war_list (
    national_id TEXT PRIMARY KEY,
    list_no     INTEGER NOT NULL,
    box         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_war_list_no ON war_list (box, list_no);
-- Who has voted (the source of truth for every list), and every mark / undo in order.
CREATE TABLE IF NOT EXISTS war_voted (
    national_id TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    by_user     INTEGER,
    tier        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS war_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    national_id TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('voted', 'undo')),
    at          TEXT NOT NULL,
    by_user     INTEGER,
    tier        TEXT NOT NULL DEFAULT '',
    client_key  TEXT,
    reason      TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_war_log_key ON war_log (client_key);
CREATE INDEX IF NOT EXISTS ix_war_log_user ON war_log (by_user, at);
-- Tier 3: who calls whom, and how the last call went.
CREATE TABLE IF NOT EXISTS war_calls (
    national_id TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 4,
    outcome     TEXT NOT NULL DEFAULT '',
    outcome_at  TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_war_calls_user ON war_calls (user_id, priority);
-- Transport: supporters who need a ride to their box. waiting -> onway (a runner took it) -> done.
CREATE TABLE IF NOT EXISTS war_transport (
    national_id  TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'onway', 'done', 'cancelled')),
    source       TEXT NOT NULL DEFAULT '',
    requested_at TEXT NOT NULL,
    requested_by INTEGER,
    runner_id    INTEGER,
    updated_at   TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_war_transport ON war_transport (status, requested_at);
-- Contact before the vote: what the agent (or the call centre) heard from the voter. Separate from the
-- portal's canvassing statuses, which stay as they are.
CREATE TABLE IF NOT EXISTS war_contact (
    national_id TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    at          TEXT NOT NULL,
    by_user     INTEGER,
    note        TEXT NOT NULL DEFAULT ''
);
-- Private links: agents who don't sign in open their War Room page with a link (no password).
CREATE TABLE IF NOT EXISTS war_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    token        TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    created_by   INTEGER,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT,
    opens        INTEGER NOT NULL DEFAULT 0,
    last_open_at TEXT
);
-- Lookups by field phones, for the rate limit and the alerts.
CREATE TABLE IF NOT EXISTS war_hits (
    user_id INTEGER NOT NULL,
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_war_hits ON war_hits (user_id, at);
"""

TIERS = {"": "No access", "room": "War Room (dashboard)", "t1": "Tier 1 · box agent", "t2": "Tier 2 · field runner",
         "t3": "Tier 3 · call centre"}
FIELD_TIERS = ("t1", "t2", "t3")
PRIORITY = {1: "Super voter", 2: "Solid base", 3: "Shared supporter", 4: "Leaning"}
OUTCOMES = {"coming": "Coming", "transport": "Needs transport", "noanswer": "No answer", "later": "Call later",
            "refused": "Refused", "wrong": "Wrong number"}
CONTACT = {"coming": "Coming", "transport": "Needs transport", "later": "Call later", "noanswer": "No answer",
           "notcoming": "Not coming"}
CALL_TO_CONTACT = {"coming": "coming", "transport": "transport", "later": "later", "noanswer": "noanswer",
                   "refused": "notcoming"}
LINK_DAYS = (3, 7, 30)
TRANSPORT = {"waiting": "Waiting", "onway": "On the way", "done": "Dropped at the box", "cancelled": "Cancelled"}
TRANSPORT_LATE = 20         # minutes a request may wait before the War Room is alerted
UNDO_MINUTES = 10
LOOKUP_LIMIT = 120          # lookups per 5 minutes per field account
STALE_MINUTES = 30
LOCAL = timedelta(hours=5)  # Maldives time

# What a field account (tiers 1-3) may open.
FIELD_ENDPOINTS = {
    "warroom.home", "warroom.box", "warroom.lookup", "warroom.mark", "warroom.calls", "warroom.call",
    "warroom.field", "warroom.changes", "warroom.transport", "warroom.transport_action", "warroom.contact",
    "warroom.link_open", "warroom.field_export", "warroom.calls_export", "voters.photo",
    "votecount.sheet", "votecount.tap", "votecount.final", "votecount.photo", "votecount.export", "theme.theme_css", "theme.set_appearance",
    "fonts.stylesheet", "fonts.serve", "auth.login", "auth.logout", "auth.change_password", "static",
}


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


# --- access ---------------------------------------------------------------------------------------

def tier(user=None):
    """'owner', 'room', 't1', 't2', 't3' or ''."""
    user = user if user is not None else g.get("user")
    if user is None:
        return ""
    if is_owner(user):
        return "owner"
    by_link = g.get("war_link") and g.get("user") is not None and g.user["id"] == user["id"]
    if (not user["can_login"] and not by_link) or user["deleted_at"] or not user["is_active"]:
        return ""
    if by_link and user["war_tier"] not in FIELD_TIERS:
        return ""
    return user["war_tier"] if user["war_tier"] in TIERS else ""


def is_field(user=None):
    """A field account: sees only its War Room page."""
    user = user if user is not None else g.get("user")
    return user is not None and user["role"] != "admin" and tier(user) in FIELD_TIERS


def is_open():
    return campaign.get_setting("war_open") == "1"


def can_watch():
    return tier() in ("owner", "room")


def field_guard():
    """Before every request: field accounts are kept to the War Room."""
    if not is_field() or request.endpoint in FIELD_ENDPOINTS:
        return None
    if request.method in ("GET", "HEAD"):
        return redirect(url_for("warroom.home"))
    audit.log("access_denied", target=request.path, detail={"war_field": True})
    abort(403)


def _need(*tiers):
    t = tier()
    if t not in tiers and t not in ("owner", "room"):
        audit.log("access_denied", target=request.path)
        abort(403)
    if t in FIELD_TIERS and not is_open():
        abort(423)  # the War Room is closed: field accounts can't do anything
    return t


def _hit():
    """Count a lookup; too many from one account in 5 minutes is refused (a phone being scraped)."""
    db = get_db()
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    n = db.execute("SELECT COUNT(*) FROM war_hits WHERE user_id = ? AND at >= ?", (g.user["id"], since)).fetchone()[0]
    if n >= LOOKUP_LIMIT and tier() in FIELD_TIERS:
        audit.log("war_rate_limit", target=g.user["username"])
        db.commit()
        abort(429)
    db.execute("INSERT INTO war_hits (user_id, at) VALUES (?, ?)", (g.user["id"], now_iso()))


# --- whose voters ---------------------------------------------------------------------------------

def supporters_sql():
    """Our supporters, best first: (SQL selecting national_id, priority), params. Priority 1 super voters
    (marked for us), 2 solid base (Yes for us only), 3 shared supporters (Yes for us and someone else),
    4 leaning (a Maybe for us and no Yes for anyone). No-shows and deceased voters are left out."""
    ours = pledges.our_candidate()
    if not ours:
        return None, []
    sup = "SELECT national_id FROM super_votes WHERE candidate_id = ?"
    yes_us = "SELECT national_id FROM pledges WHERE value = 'yes' AND candidate_id = ?"
    yes_any = "SELECT national_id FROM pledges WHERE value = 'yes'"
    maybe_us = "SELECT national_id FROM pledges WHERE value = 'maybe' AND candidate_id = ?"
    only_us = ("SELECT national_id FROM pledges WHERE value = 'yes' GROUP BY national_id"
               " HAVING COUNT(*) = 1 AND MAX(candidate_id) = ?")
    sql = (f"SELECT v.national_id, CASE WHEN v.national_id IN ({sup}) THEN 1"
           f" WHEN v.national_id IN ({only_us}) THEN 2 WHEN v.national_id IN ({yes_us}) THEN 3 ELSE 4 END AS priority"
           " FROM voters v WHERE v.deceased = 0 AND v.national_id NOT IN (SELECT national_id FROM pledge_noshow)"
           f" AND (v.national_id IN ({sup}) OR v.national_id IN ({yes_us})"
           f"  OR (v.national_id IN ({maybe_us}) AND v.national_id NOT IN ({yes_any})))")
    params = [ours] * 6
    clause, extra = campaign.party_scope("v.party")
    if clause:
        sql += f" AND {clause}"
        params += list(extra)
    return sql, params


def _scope_sql(t, user_id, box):
    """Voters a tier may see: (SQL condition on national_id, params)."""
    from .voters import handled_by

    if t == "t1":
        return "national_id IN (SELECT national_id FROM voters WHERE location = ?)", [box]
    if t == "t2":
        return handled_by(), [user_id, user_id]
    if t == "t3":
        return "national_id IN (SELECT national_id FROM war_calls WHERE user_id = ?)", [user_id]
    return "1", []


def _box_of(t):
    if t == "t1":
        return g.user["war_box"] or ""
    return request.args.get("box", request.form.get("box", ""))[:200]


def _voter_card(v, t):
    """What a field phone gets about one voter: no party, no pledges; phones only for tiers 2 and 3."""
    db = get_db()
    no = db.execute("SELECT list_no FROM war_list WHERE national_id = ?", (v["national_id"],)).fetchone()
    voted = db.execute("SELECT at FROM war_voted WHERE national_id = ?", (v["national_id"],)).fetchone()
    return {"national_id": v["national_id"], "name": v["full_name"], "house": v["house_name"], "box": v["location"],
            "sex": v["sex"], "age": v["age"], "list_no": no[0] if no else None,
            "phone": v["phone"] if t not in ("t1",) else "",
            "photo": url_for("voters.photo", national_id=v["national_id"]) if v["photo_path"] else "",
            "voted": bool(voted), "voted_at": _hm(voted[0]) if voted else ""}


def _hm(stamp):
    try:
        return (datetime.fromisoformat(stamp) + LOCAL).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


# --- pages ----------------------------------------------------------------------------------------

@bp.route("/war")
@login_required
def home():
    t = tier()
    if t == "t1":
        return redirect(url_for("warroom.box"))
    if t == "t2":
        return redirect(url_for("warroom.field"))
    if t == "t3":
        return redirect(url_for("warroom.calls"))
    if t not in ("owner", "room"):
        if g.user["role"] == "admin":
            return redirect(url_for("warroom.team"))
        abort(403)
    ag = agent_filter()[2]
    agents = get_db().execute("SELECT id, username FROM users WHERE role IN ('agent', 'user', 'admin') AND deleted_at IS NULL"
                              " ORDER BY username COLLATE NOCASE").fetchall()
    return render_template("warroom/dashboard.html", s=stats(), tiers=TIERS, is_open=is_open(), cursor=_cursor(),
                           outcomes=OUTCOMES, priority=PRIORITY, ag=ag, agents=agents, contact=CONTACT,
                           ag_name=next((a["username"] for a in agents if str(a["id"]) == ag), "No agent" if ag == "none" else ""))


def _closed():
    return render_template("warroom/closed.html"), 423


@bp.route("/war/box")
@login_required
def box():
    """Tier 1: type the number the official calls out, check the face, tap Voted."""
    t = tier()
    if t not in ("t1", "owner", "room"):
        abort(403)
    if t == "t1" and not is_open():
        return _closed()
    db = get_db()
    b = _box_of(t)
    boxes = [r[0] for r in db.execute("SELECT DISTINCT location FROM voters WHERE location != '' ORDER BY location")]
    if t != "t1" and not b and boxes:
        b = boxes[0]
    total = db.execute("SELECT COUNT(*) FROM voters WHERE location = ? AND deceased = 0", (b,)).fetchone()[0]
    voted = db.execute("SELECT COUNT(*) FROM war_voted w JOIN voters v ON v.national_id = w.national_id"
                       " WHERE v.location = ?", (b,)).fetchone()[0]
    recent = db.execute(
        "SELECT v.national_id, v.full_name, w.at, l.list_no, w.by_user FROM war_voted w JOIN voters v"
        " ON v.national_id = w.national_id LEFT JOIN war_list l ON l.national_id = v.national_id"
        " WHERE v.location = ? ORDER BY w.at DESC LIMIT 15", (b,)).fetchall()
    has_numbers = db.execute("SELECT 1 FROM war_list WHERE box = ? LIMIT 1", (b,)).fetchone() is not None
    return render_template("warroom/box.html", box=b, boxes=boxes if t != "t1" else [], total=total, voted=voted,
                           recent=recent, has_numbers=has_numbers, t=t, cursor=_cursor(), is_open=is_open(),
                           undo_minutes=UNDO_MINUTES, hm=_hm, me=g.user["id"],
                           undo_after=(datetime.now(timezone.utc) - timedelta(minutes=UNDO_MINUTES)).isoformat(timespec="seconds"))


@bp.route("/war/lookup")
@login_required
def lookup():
    """One voter at the agent's box, by exact list number or exact ID card number. Nothing else."""
    t = _need("t1", "t2")
    _hit()
    db = get_db()
    q = (request.args.get("q") or "").strip()
    b = _box_of(t)
    v = None
    if q.isdigit():
        v = db.execute("SELECT v.* FROM war_list l JOIN voters v ON v.national_id = l.national_id"
                       " WHERE l.list_no = ? AND v.location = ?", (int(q), b)).fetchone()
    if v is None:
        nid, _ = normalize_id(q)
        if nid:
            v = db.execute("SELECT * FROM voters WHERE national_id = ? AND location = ?", (nid, b)).fetchone()
    db.commit()
    if v is None:
        return jsonify({"ok": False, "error": f"No voter {q} at {b or 'this box'}."}), 404
    return jsonify({"ok": True, "voter": _voter_card(v, t)})


@bp.post("/war/mark")
@login_required
def mark():
    """Mark a voter voted (value=1) or undo it (0). Safe to resend: the same key is counted once."""
    t = _need("t1", "t2")
    db = get_db()
    nid = request.form.get("national_id", "")
    key = (request.form.get("key") or "")[:64] or None
    if key and db.execute("SELECT 1 FROM war_log WHERE client_key = ?", (key,)).fetchone():
        return jsonify({"ok": True, "repeat": True})
    if t in FIELD_TIERS:
        cond, params = _scope_sql(t, g.user["id"], _box_of(t))
        if not db.execute(f"SELECT 1 FROM voters WHERE national_id = ? AND {cond}", [nid] + params).fetchone():
            audit.log("access_denied", target=nid, detail={"war_mark": t})
            abort(403)
    elif not db.execute("SELECT 1 FROM voters WHERE national_id = ?", (nid,)).fetchone():
        abort(404)
    wanted = request.form.get("value", "1")
    if wanted not in ("0", "1"):
        abort(400)
    row = db.execute("SELECT * FROM war_voted WHERE national_id = ?", (nid,)).fetchone()
    if wanted == "1" and row is None:
        db.execute("INSERT INTO war_voted (national_id, at, by_user, tier) VALUES (?, ?, ?, ?)",
                   (nid, now_iso(), g.user["id"], t))
        db.execute("INSERT INTO war_log (national_id, action, at, by_user, tier, client_key) VALUES (?, 'voted', ?, ?, ?, ?)",
                   (nid, now_iso(), g.user["id"], t, key))
    elif wanted == "0" and row is not None:
        # a field agent can undo only their own mark, for a few minutes; the War Room any time
        recent = datetime.fromisoformat(row["at"]) > datetime.now(timezone.utc) - timedelta(minutes=UNDO_MINUTES)
        if t in FIELD_TIERS and (row["by_user"] != g.user["id"] or not recent):
            return jsonify({"ok": False, "error": "Only the War Room can undo this now."}), 403
        db.execute("DELETE FROM war_voted WHERE national_id = ?", (nid,))
        db.execute("INSERT INTO war_log (national_id, action, at, by_user, tier, client_key, reason)"
                   " VALUES (?, 'undo', ?, ?, ?, ?, ?)",
                   (nid, now_iso(), g.user["id"], t, key, clean_text(request.form.get("reason", ""))[:200]))
    db.commit()
    return jsonify({"ok": True, "voted": wanted == "1"})


@bp.route("/war/changes")
@login_required
def changes():
    """What changed since ?since=<cursor>, within what this account may see: voted and undone IDs."""
    t = tier()
    if not t:
        abort(403)
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    db = get_db()
    cond, params = _scope_sql(t, g.user["id"], _box_of(t)) if t in FIELD_TIERS else ("1", [])
    rows = db.execute(f"SELECT id, national_id, action FROM war_log WHERE id > ? AND national_id IN"
                      f" (SELECT national_id FROM voters WHERE {cond}) ORDER BY id LIMIT 2000",
                      [since] + params).fetchall()
    out = {"cursor": max([since] + [r["id"] for r in rows]) if rows else _cursor(), "voted": [], "undone": []}
    if rows and len(rows) < 2000:
        out["cursor"] = max(_cursor(), out["cursor"])
    for r in rows:
        (out["voted"] if r["action"] == "voted" else out["undone"]).append(r["national_id"])
    out["open"] = is_open()
    if t in ("t2", "owner", "room"):
        out["trev"] = _transport_rev()
    if t in ("owner", "room") and request.args.get("stats"):
        out["stats"] = _flat(stats())
    return jsonify(out)


def _cursor():
    return get_db().execute("SELECT COALESCE(MAX(id), 0) FROM war_log").fetchone()[0]


@bp.route("/war/calls")
@login_required
def calls():
    """Tier 3: my call list, supporters who haven't voted yet, best first."""
    t = tier()
    if t not in ("t3", "owner", "room"):
        abort(403)
    if t == "t3" and not is_open():
        return _closed()
    db = get_db()
    who = g.user["id"] if t == "t3" else request.args.get("u", type=int)
    callers = db.execute("SELECT id, username FROM users WHERE war_tier = 't3' AND deleted_at IS NULL"
                         " ORDER BY username COLLATE NOCASE").fetchall()
    if who is None and callers:
        who = callers[0]["id"]
    rows = db.execute(
        "SELECT v.national_id, v.full_name, v.house_name, v.phone, v.location, v.photo_path, c.priority, c.outcome,"
        " c.outcome_at, c.attempts FROM war_calls c JOIN voters v ON v.national_id = c.national_id"
        " WHERE c.user_id = ? AND c.national_id NOT IN (SELECT national_id FROM war_voted)"
        " ORDER BY c.outcome = 'coming', c.priority, c.attempts, v.house_name COLLATE NOCASE", (who or 0,)).fetchall()
    done = db.execute("SELECT COUNT(*) FROM war_calls WHERE user_id = ? AND national_id IN"
                      " (SELECT national_id FROM war_voted)", (who or 0,)).fetchone()[0]
    return render_template("warroom/calls.html", rows=rows, done=done, outcomes=OUTCOMES, priority=PRIORITY, t=t,
                           callers=callers if t != "t3" else [], who=who, cursor=_cursor(), hm=_hm)


@bp.route("/war/calls/export.<fmt>")
@login_required
def calls_export(fmt):
    """A caller's list (or every caller's) as CSV / Excel: who still has to vote, and how each call went.
    A tier 3 caller gets only their own list, while the War Room is open."""
    from flask import Response

    from .voters import _safe_cell

    if fmt not in ("csv", "xlsx"):
        abort(404)
    t = _need("t3")
    who = request.args.get("u", "")
    who = "all" if who == "all" and t != "t3" else (int(who) if who.isdigit() else 0)
    if t == "t3":
        who = g.user["id"]
    db = get_db()
    cond, params = ("1", []) if who == "all" else ("c.user_id = ?", [who])
    rows = db.execute(
        "SELECT v.national_id, v.full_name, v.house_name, v.phone, v.location, v.age, c.priority, c.outcome,"
        " c.outcome_at, c.attempts, u.username AS caller, k.status AS contact FROM war_calls c"
        " JOIN voters v ON v.national_id = c.national_id LEFT JOIN users u ON u.id = c.user_id"
        " LEFT JOIN war_contact k ON k.national_id = c.national_id"
        f" WHERE {cond} AND c.national_id NOT IN (SELECT national_id FROM war_voted)"
        " ORDER BY u.username COLLATE NOCASE, c.outcome = 'coming', c.priority, c.attempts, v.house_name COLLATE NOCASE",
        params).fetchall()
    headers = ["#", "Caller", "ID", "Name", "House", "Box", "Phone", "Age", "Group", "Last call", "When", "Calls",
               "Said (field / calls)"]
    table = [[n, r["caller"] or "", r["national_id"], _safe_cell(r["full_name"]), _safe_cell(r["house_name"]),
              _safe_cell(r["location"]), r["phone"], r["age"], PRIORITY.get(r["priority"], ""),
              OUTCOMES.get(r["outcome"], "Not called yet"), _local_day(r["outcome_at"]) if r["outcome_at"] else "",
              r["attempts"], CONTACT.get(r["contact"], "")] for n, r in enumerate(rows, 1)]
    audit.log("war_calls_export", target=fmt, detail={"caller": who, "rows": len(table)})
    name = f"calls-{'all' if who == 'all' else who}-{datetime.now().strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "csv":
        import csv

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(table)
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Calls"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in table:
        ws.append(row)
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), headers={"Content-Disposition": f'attachment; filename="{name}"'},
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.post("/war/call")
@login_required
def call():
    t = _need("t3")
    db = get_db()
    nid = request.form.get("national_id", "")
    outcome = request.form.get("outcome", "")
    if outcome not in OUTCOMES:
        abort(400)
    row = db.execute("SELECT * FROM war_calls WHERE national_id = ?", (nid,)).fetchone()
    if row is None or (t == "t3" and row["user_id"] != g.user["id"]):
        abort(403)
    db.execute("UPDATE war_calls SET outcome = ?, outcome_at = ?, attempts = attempts + 1 WHERE national_id = ?",
               (outcome, now_iso(), nid))
    if outcome == "transport":
        request_transport(nid, "call")
    if outcome in CALL_TO_CONTACT:
        set_contact(nid, CALL_TO_CONTACT[outcome])
    audit.log("war_call", target=nid, detail={"outcome": outcome}, commit=False)
    db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "outcome": outcome, "label": OUTCOMES[outcome]})
    return redirect(url_for("warroom.calls"))


@bp.route("/war/field")
@login_required
def field():
    """Tier 2: my supporters who haven't voted. Reach each one before the vote and set what they said:
    coming, needs transport, call later, no answer or not coming."""
    t = tier()
    if t not in ("t2", "owner", "room"):
        abort(403)
    if t == "t2" and not is_open():
        return _closed()
    db = get_db()
    sql, sp = supporters_sql()
    who = g.user["id"] if t == "t2" else request.args.get("u", type=int)
    runners = db.execute("SELECT id, username FROM users WHERE war_tier = 't2' AND deleted_at IS NULL"
                         " ORDER BY username COLLATE NOCASE").fetchall()
    if who is None and runners:
        who = runners[0]["id"]
    cs = request.args.get("cs", "")
    cs = cs if cs in CONTACT or cs == "none" else ""
    rows, counts = runner_rows(who or 0, cs)
    return render_template("warroom/field.html", rows=rows, priority=PRIORITY, outcomes=OUTCOMES, t=t,
                           runners=runners if t != "t2" else [], who=who, cursor=_cursor(), contact=CONTACT,
                           counts=counts, cs=cs, total=sum(counts.values()), transport_labels=TRANSPORT,
                           trev=_transport_rev(), hm=_hm)


def runner_rows(who, cs=""):
    """A runner's supporters who haven't voted (who='all': every runner's), and the counts per contact status."""
    from .voters import handled_by

    db = get_db()
    sql, sp = supporters_sql()
    if not sql:
        return [], {}
    if who == "all":
        runners = [r[0] for r in db.execute("SELECT id FROM users WHERE war_tier = 't2' AND deleted_at IS NULL")]
        if not runners:
            return [], {}
        cond = " OR ".join([handled_by()] * len(runners))
        params = [x for r in runners for x in (r, r)]
    else:
        cond, params = _scope_sql("t2", who, "")
    base = (f"FROM ({sql}) s JOIN voters v ON v.national_id = s.national_id"
            " LEFT JOIN war_contact k ON k.national_id = v.national_id"
            " LEFT JOIN war_calls c ON c.national_id = v.national_id"
            " LEFT JOIN war_transport tr ON tr.national_id = v.national_id AND tr.status IN ('waiting', 'onway')"
            " LEFT JOIN voter_assignments va ON va.national_id = v.national_id"
            " LEFT JOIN house_assignments ha ON ha.house_name = v.house_name"
            " LEFT JOIN users ru ON ru.id = COALESCE(va.user_id, ha.user_id)"
            f" WHERE v.national_id IN (SELECT national_id FROM voters WHERE {cond})"
            " AND v.national_id NOT IN (SELECT national_id FROM war_voted)")
    counts = dict(db.execute(f"SELECT COALESCE(k.status, 'none'), COUNT(*) {base} GROUP BY 1", sp + params).fetchall())
    where = "" if not cs else (" AND k.status IS NULL" if cs == "none" else " AND k.status = ?")
    rows = db.execute(
        f"SELECT v.*, s.priority, c.outcome, k.status AS contact, k.at AS contact_at, k.by_user AS contact_by,"
        f" tr.status AS ride, ru.username AS runner {base}{where}"
        " ORDER BY CASE COALESCE(k.status, '') WHEN 'transport' THEN 0 WHEN '' THEN 1 WHEN 'later' THEN 2"
        " WHEN 'noanswer' THEN 3 WHEN 'notcoming' THEN 4 ELSE 5 END, s.priority, v.house_name COLLATE NOCASE, v.full_name",
        sp + params + ([cs] if cs and cs != "none" else [])).fetchall()
    return rows, counts


@bp.route("/war/field/export.<fmt>")
@login_required
def field_export(fmt):
    """A runner's list (or every runner's) as CSV / Excel, with what each voter said. A tier 2 runner can
    download only their own list (while the War Room is open); the War Room any runner's or all."""
    from flask import Response

    from .voters import _safe_cell

    if fmt not in ("csv", "xlsx"):
        abort(404)
    t = _need("t2")
    who = request.args.get("u", "")
    who = "all" if who == "all" else (int(who) if who.isdigit() else 0)
    if t == "t2":
        who = g.user["id"]  # their own list, whatever is asked
    cs = request.args.get("cs", "")
    cs = cs if cs in CONTACT or cs == "none" else ""
    rows, _ = runner_rows(who, cs)
    headers = ["#", "Runner", "ID", "Name", "House", "Box", "Phone", "Age", "Group", "Said", "When", "Transport",
               "Call centre"]
    table = [[n, r["runner"] or "", r["national_id"], _safe_cell(r["full_name"]), _safe_cell(r["house_name"]),
              _safe_cell(r["location"]), r["phone"], r["age"], PRIORITY.get(r["priority"], ""),
              CONTACT.get(r["contact"], "Not reached yet"), _local_day(r["contact_at"]) if r["contact_at"] else "",
              TRANSPORT.get(r["ride"], ""), OUTCOMES.get(r["outcome"], "")] for n, r in enumerate(rows, 1)]
    audit.log("war_field_export", target=fmt, detail={"runner": who, "status": cs, "rows": len(table)})
    name = f"supporters-{'all' if who == 'all' else who}-{datetime.now().strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "csv":
        import csv

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(table)
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Supporters"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in table:
        ws.append(row)
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), headers={"Content-Disposition": f'attachment; filename="{name}"'},
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --- the dashboard ----------------------------------------------------------------------------------

def agent_filter():
    """?ag=<user id> or none: limit the dashboard to one agent's voters (SQL on voters, params, value)."""
    from .voters import NO_AGENT, handled_by

    ag = request.args.get("ag", "")
    if ag == "none":
        return NO_AGENT, [], ag
    if ag.isdigit():
        return handled_by(), [int(ag), int(ag)], ag
    return None, [], ""


def stats():
    db = get_db()
    now = datetime.now(timezone.utc)
    clause, extra = campaign.party_scope()
    scope = f" AND {clause}" if clause else ""
    acond, aparams, _ = agent_filter()
    if acond:
        scope += f" AND {acond}"
        extra = list(extra) + aparams
    mine = f" AND national_id IN (SELECT national_id FROM voters WHERE deceased = 0{scope})"
    total = db.execute(f"SELECT COUNT(*) FROM voters WHERE deceased = 0{scope}", extra).fetchone()[0]
    voted = db.execute(f"SELECT COUNT(*) FROM war_voted WHERE national_id IN (SELECT national_id FROM voters"
                       f" WHERE deceased = 0{scope})", extra).fetchone()[0]
    sql, sp = supporters_sql()
    groups = {k: {"total": 0, "voted": 0} for k in PRIORITY}
    by_box = {}
    if sql:
        for r in db.execute(f"SELECT s.priority, v.location, s.national_id IN (SELECT national_id FROM war_voted) AS done"
                            f" FROM ({sql}) s JOIN voters v ON v.national_id = s.national_id"
                            f" WHERE 1{mine.replace('national_id IN', 's.national_id IN', 1)}", sp + list(extra)):
            groups[r[0]]["total"] += 1
            groups[r[0]]["voted"] += r[2]
            b = by_box.setdefault(r[1] or "", {"sup": 0, "sup_voted": 0})
            b["sup"] += 1
            b["sup_voted"] += r[2]
    box_last = dict(db.execute("SELECT v.location, MAX(w.at) FROM war_voted w JOIN voters v ON v.national_id = w.national_id"
                               f" WHERE 1{mine.replace('national_id IN', 'w.national_id IN', 1)} GROUP BY v.location",
                               extra).fetchall())
    boxes = []
    for r in db.execute(f"SELECT location, COUNT(*), SUM(national_id IN (SELECT national_id FROM war_voted))"
                        f" FROM voters WHERE deceased = 0{scope} GROUP BY location ORDER BY COUNT(*) DESC", extra):
        b = by_box.get(r[0] or "", {"sup": 0, "sup_voted": 0})
        boxes.append({"box": r[0] or "", "total": r[1], "voted": r[2] or 0, "sup": b["sup"], "sup_voted": b["sup_voted"]})
    sup_total = sum(x["total"] for x in groups.values())
    sup_voted = sum(x["voted"] for x in groups.values())
    # hourly turnout, Maldives time
    hours = {}
    for (at,) in db.execute(f"SELECT at FROM war_voted WHERE 1{mine}", extra):
        try:
            t = (datetime.fromisoformat(at) + LOCAL).replace(minute=0, second=0, microsecond=0)
        except ValueError:
            continue
        hours[t] = hours.get(t, 0) + 1
    hourly = [(t.strftime("%H:00"), n) for t, n in sorted(hours.items())]
    # agents
    agents = []
    for u in db.execute("SELECT id, username, war_tier, war_box, last_login_at FROM users WHERE war_tier IN"
                        " ('t1', 't2', 't3') AND deleted_at IS NULL ORDER BY war_tier, username COLLATE NOCASE"):
        marks = db.execute("SELECT COUNT(*), MAX(at) FROM war_log WHERE by_user = ? AND action = 'voted'", (u["id"],)).fetchone()
        calls_made = db.execute("SELECT COUNT(*), MAX(outcome_at) FROM war_calls WHERE user_id = ? AND outcome != ''",
                                (u["id"],)).fetchone()
        assigned = db.execute("SELECT COUNT(*) FROM war_calls WHERE user_id = ? AND national_id NOT IN"
                              " (SELECT national_id FROM war_voted)", (u["id"],)).fetchone()[0]
        hits = db.execute("SELECT COUNT(*) FROM war_hits WHERE user_id = ? AND at >= ?",
                          (u["id"], (now - timedelta(minutes=5)).isoformat(timespec="seconds"))).fetchone()[0]
        last = max([x for x in (marks[1], calls_made[1]) if x] or [""])
        stale = bool(is_open() and (not last or datetime.fromisoformat(last) < now - timedelta(minutes=STALE_MINUTES)))
        agents.append({"id": u["id"], "username": u["username"], "tier": u["war_tier"], "box": u["war_box"],
                       "marks": marks[0], "calls": calls_made[0], "pending": assigned, "last": _hm(last) if last else "",
                       "stale": stale, "hot": hits >= LOOKUP_LIMIT * 0.8})
    outcomes = dict(db.execute("SELECT outcome, COUNT(*) FROM war_calls WHERE outcome != '' AND national_id NOT IN"
                               f" (SELECT national_id FROM war_voted){mine} GROUP BY outcome", extra).fetchall())
    called = db.execute(f"SELECT COUNT(*) FROM war_calls WHERE 1{mine}", extra).fetchone()[0]
    alerts = []
    turnout = voted / total if total else 0
    for b in boxes:
        if b["sup"] >= 10 and b["voted"] and b["sup_voted"] / b["sup"] < turnout - 0.10:
            alerts.append(f"{b['box'] or 'No box'}: our supporters {round(100 * b['sup_voted'] / b['sup'])}% voted,"
                          f" box turnout {round(100 * b['voted'] / b['total'])}%.")
    for a in agents:
        if a["stale"]:
            alerts.append(f"{a['username']} ({TIERS[a['tier']].split(' · ')[0]}): nothing for {STALE_MINUTES}+ minutes.")
        if a["hot"]:
            alerts.append(f"{a['username']}: very many lookups in 5 minutes. Check the phone.")
    # transport
    tr = transport_counts(mine, extra)
    if tr["late"]:
        alerts.append(f"{tr['late']} transport request{'s' if tr['late'] != 1 else ''} waiting more than"
                      f" {TRANSPORT_LATE} minutes (oldest {tr['oldest']} min). Send a runner.")
    # boxes gone quiet
    if is_open():
        for b in boxes:
            last = box_last.get(b["box"])
            if b["voted"] and last and datetime.fromisoformat(last) < now - timedelta(minutes=STALE_MINUTES):
                alerts.append(f"{b['box'] or 'No box'}: no vote marked for {STALE_MINUTES}+ minutes. Is the box agent there?")
    # countdown to the polls closing
    close = campaign.get_setting("war_close") or "16:00"
    local_now = now + LOCAL
    try:
        hh, mm = (int(x) for x in close.split(":"))
        closes = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        left = int((closes - local_now).total_seconds() // 60)
    except ValueError:
        left = None
    if left is not None and 0 < left <= 60 and sup_total - sup_voted:
        alerts.insert(0, f"{left} minutes to close: {sup_total - sup_voted} of our supporters still to vote.")
    return {"total": total, "voted": voted, "pct": round(100 * turnout, 1), "sup_total": sup_total,
            "sup_voted": sup_voted, "sup_pending": sup_total - sup_voted,
            "sup_pct": round(100 * sup_voted / sup_total, 1) if sup_total else 0, "groups": groups, "boxes": boxes,
            "hourly": hourly, "hour_max": max([n for _, n in hourly] or [0]), "agents": agents, "outcomes": outcomes,
            "called": called, "alerts": alerts, "close": close, "ours": pledges.our_candidate(),
            "now": local_now.strftime("%H:%M"), "transport": tr,
            "left": ("closed" if left is not None and left <= 0 else f"{left // 60}h {left % 60:02d}m" if left is not None else ""),
            "projection": projection(scope, extra), "contact": contact_counts(mine, extra)}


def _flat(s):
    """The numbers the dashboard updates in place, keyed like its data-k attributes."""
    out = {k: s[k] for k in ("total", "voted", "pct", "sup_total", "sup_voted", "sup_pending", "sup_pct", "now", "left")}
    out["t_waiting"], out["t_onway"], out["t_done"] = (s["transport"][k] for k in ("waiting", "onway", "done"))
    for k, x in s["groups"].items():
        out[f"g{k}v"], out[f"g{k}p"] = x["voted"], x["total"] - x["voted"]
    for i, b in enumerate(s["boxes"]):
        out[f"b{i}v"], out[f"b{i}p"] = b["voted"], round(100 * b["voted"] / b["total"]) if b["total"] else 0
        out[f"b{i}sv"], out[f"b{i}sp"] = b["sup_voted"], b["sup"] - b["sup_voted"]
    for code, n in s["outcomes"].items():
        out[f"o_{code}"] = n
    for code, n in s["contact"].items():
        out[f"c_{code or 'none'}"] = n
    return out


# --- the team (owner) -------------------------------------------------------------------------------

@bp.route("/war/team", methods=["GET", "POST"])
@login_required
def team():
    """Who does what on the day. Admins give tiers and links and make the call lists; only the owner
    opens or closes the War Room, gives the War Room dashboard and prints the sheets."""
    owner = is_owner()
    if not owner and g.user["role"] != "admin":
        abort(403)
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action in ("open", "close", "times") and not owner:
            audit.log("access_denied", target=request.path, detail={"action": action})
            abort(403)
        if action == "set":
            uid = request.form.get("user_id", type=int)
            user = db.execute("SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (uid,)).fetchone() or abort(404)
            if is_owner(user):
                abort(400)
            level = request.form.get("tier", "")
            if not owner and "room" in (level, user["war_tier"]):
                flash("Only the owner gives (or takes away) the War Room dashboard.", "error")
                return redirect(url_for("warroom.team"))
            if level not in TIERS or (level in FIELD_TIERS and user["role"] == "admin"):
                flash("Admins can be given the War Room dashboard, not a field tier.", "error")
                return redirect(url_for("warroom.team"))
            b = request.form.get("war_box", "")[:200]
            db.execute("UPDATE users SET war_tier = ?, war_box = ? WHERE id = ?", (level, b if level == "t1" else "", uid))
            if level not in FIELD_TIERS:  # no field tier: their private links stop
                db.execute("UPDATE war_links SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now_iso(), uid))
            audit.log("war_tier", target=user["username"], detail={"tier": level, "box": b}, commit=False)
            flash(f"{user['username']}: {TIERS[level]}{' · ' + b if level == 't1' and b else ''}.", "success")
        elif action in ("open", "close"):
            campaign.put_setting("war_open", "1" if action == "open" else "0")
            audit.log("war_" + action, commit=False)
            flash("War Room open: field accounts can work now." if action == "open" else
                  "War Room closed: field accounts can't see or mark anything.", "success")
        elif action == "times":
            close = request.form.get("close", "")
            if re.fullmatch(r"\d{2}:\d{2}", close):
                campaign.put_setting("war_close", close)
                flash("Saved.", "success")
        elif action == "allocate":
            n = allocate()
            flash(f"Call lists made: {n} supporters shared between the callers, best first.", "success")
        elif action == "move":
            a, b = request.form.get("from", type=int), request.form.get("to", type=int)
            if a and b and a != b:
                moved = db.execute("UPDATE war_calls SET user_id = ? WHERE user_id = ? AND national_id NOT IN"
                                   " (SELECT national_id FROM war_voted)", (b, a)).rowcount
                audit.log("war_move", detail={"from": a, "to": b, "n": moved}, commit=False)
                flash(f"Moved {moved} call{'s' if moved != 1 else ''}.", "success")
        elif action == "transport_from_visits":
            n = transport_from_visits()
            flash(f"{n} supporter{'s' if n != 1 else ''} from houses flagged 'needs transport' added to the transport queue.",
                  "success")
        elif action == "link":
            uid = request.form.get("user_id", type=int)
            user = db.execute("SELECT * FROM users WHERE id = ? AND deleted_at IS NULL", (uid,)).fetchone() or abort(404)
            days = request.form.get("days", type=int)
            if user["war_tier"] not in FIELD_TIERS or user["role"] == "admin":
                flash("Give the agent a tier first.", "error")
            elif days not in LINK_DAYS:
                abort(400)
            else:
                make_link(user, days)
                flash(f"New private link for {user['username']}: copy it below and send it to them.", "success")
        elif action == "unlink":
            uid = request.form.get("user_id", type=int)
            n = db.execute("UPDATE war_links SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now_iso(), uid)).rowcount
            audit.log("war_link_stop", target=str(uid), commit=False)
            flash("Link stopped: it doesn't open any more." if n else "There was no live link.", "success")
        elif action == "import":
            try:
                n, missing = import_numbers(request.files.get("sheet"))
                flash(f"Official list numbers saved for {n} voters." + (f" {missing} IDs weren't in the portal." if missing else ""),
                      "success")
            except ValueError as exc:
                flash(str(exc), "error")
        db.commit()
        return redirect(url_for("warroom.team"))
    users = db.execute("SELECT * FROM users WHERE deleted_at IS NULL AND (can_login = 1 OR role = 'agent')"
                       " ORDER BY role != 'agent', username COLLATE NOCASE").fetchall()
    now = now_iso()
    links = {r["user_id"]: r for r in db.execute(
        "SELECT * FROM war_links WHERE revoked_at IS NULL AND expires_at > ? ORDER BY id", (now,))}
    boxes = [r[0] for r in db.execute("SELECT DISTINCT location FROM voters WHERE location != '' ORDER BY location")]
    loads = dict(db.execute("SELECT user_id, COUNT(*) FROM war_calls WHERE national_id NOT IN"
                            " (SELECT national_id FROM war_voted) GROUP BY user_id").fetchall())
    numbered = db.execute("SELECT COUNT(*) FROM war_list").fetchone()[0]
    return render_template("warroom/team.html", users=users, tiers=TIERS, boxes=boxes, loads=loads, is_open=is_open(),
                           close=campaign.get_setting("war_close") or "16:00", numbered=numbered,
                           ours=pledges.our_candidate(), owner_id=next((u["id"] for u in users if is_owner(u)), None),
                           can_edit=True, can_own=owner, can_watch=can_watch(), links=links, link_days=LINK_DAYS,
                           link_url=lambda r: url_for("warroom.link_open", token=r["token"], _external=True),
                           contact=CONTACT, local=_local_day)


def allocate():
    """Share our supporters who haven't voted between the tier 3 callers, best first, keeping lists
    already made (and their call outcomes); newcomers go to whoever has the shortest list."""
    db = get_db()
    callers = [r[0] for r in db.execute("SELECT id FROM users WHERE war_tier = 't3' AND deleted_at IS NULL"
                                        " AND is_active = 1 ORDER BY id")]
    sql, sp = supporters_sql()
    if not callers or not sql:
        return 0
    db.execute(f"DELETE FROM war_calls WHERE user_id NOT IN ({','.join('?' * len(callers))})", callers)
    load = {c: 0 for c in callers}
    for uid, n in db.execute("SELECT user_id, COUNT(*) FROM war_calls GROUP BY user_id"):
        load[uid] = n
    have = {r[0] for r in db.execute("SELECT national_id FROM war_calls")}
    rows = db.execute(f"SELECT s.national_id, s.priority FROM ({sql}) s JOIN voters v ON v.national_id = s.national_id"
                      " WHERE s.national_id NOT IN (SELECT national_id FROM war_voted)"
                      " ORDER BY s.priority, v.location, v.house_name", sp).fetchall()
    added = 0
    for nid, prio in rows:
        if nid in have:
            db.execute("UPDATE war_calls SET priority = ? WHERE national_id = ?", (prio, nid))
            continue
        who = min(load, key=lambda c: (load[c], c))
        db.execute("INSERT INTO war_calls (national_id, user_id, priority) VALUES (?, ?, ?)", (nid, who, prio))
        load[who] += 1
        added += 1
    audit.log("war_allocate", detail={"added": added, "callers": len(callers)}, commit=False)
    return len(rows)


def import_numbers(upload):
    """The official list: an ID column and a number column (No., #, Serial...). Box from the portal."""
    if upload is None or not upload.filename:
        raise ValueError("Choose the official list (Excel or CSV).")
    rows = pledges._read_table(upload)
    if not rows:
        raise ValueError("The file is empty.")
    head = [str(h or "").strip().lower() for h in rows[0]]
    id_col = next((i for i, h in enumerate(head) if h in ("id", "id card", "national id", "id no", "id number", "nid")), None)
    no_col = next((i for i, h in enumerate(head) if h in ("no", "no.", "#", "number", "serial", "serial no", "list no",
                                                          "list number", "sl", "sl no")), None)
    if id_col is None or no_col is None:
        raise ValueError("The first row needs an ID column and a number column (No.).")
    db = get_db()
    boxes = dict(db.execute("SELECT national_id, location FROM voters").fetchall())
    n = missing = 0
    for row in rows[1:]:
        if max(id_col, no_col) >= len(row):
            continue
        nid, _ = normalize_id(str(row[id_col] or ""))
        no = str(row[no_col] or "").strip().split(".")[0]
        if not nid or not no.isdigit():
            continue
        if nid not in boxes:
            missing += 1
            continue
        db.execute("INSERT INTO war_list (national_id, list_no, box) VALUES (?, ?, ?) ON CONFLICT(national_id)"
                   " DO UPDATE SET list_no = excluded.list_no, box = excluded.box", (nid, int(no), boxes[nid]))
        n += 1
    audit.log("war_import_numbers", detail={"n": n, "missing": missing}, commit=False)
    return n, missing



# --- transport ------------------------------------------------------------------------------------

def request_transport(nid, source, by=None):
    """Put a supporter in the transport queue (again, if an earlier request was cancelled)."""
    db = get_db()
    if db.execute("SELECT 1 FROM war_voted WHERE national_id = ?", (nid,)).fetchone():
        return False
    row = db.execute("SELECT status FROM war_transport WHERE national_id = ?", (nid,)).fetchone()
    if row and row["status"] in ("waiting", "onway"):
        return False
    db.execute("INSERT INTO war_transport (national_id, status, source, requested_at, requested_by, updated_at)"
               " VALUES (?, 'waiting', ?, ?, ?, ?) ON CONFLICT(national_id) DO UPDATE SET status = 'waiting',"
               " source = excluded.source, requested_at = excluded.requested_at, requested_by = excluded.requested_by,"
               " runner_id = NULL, updated_at = excluded.updated_at",
               (nid, source, now_iso(), by if by is not None else g.user["id"], now_iso()))
    return True


def _transport_rev():
    row = get_db().execute("SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM war_transport").fetchone()
    voted = get_db().execute("SELECT COUNT(*) FROM war_transport t JOIN war_voted w ON w.national_id = t.national_id"
                             " WHERE t.status IN ('waiting', 'onway')").fetchone()[0]
    return f"{row[0]}-{row[1]}-{voted}"


def transport_counts(mine="", extra=()):
    db = get_db()
    now = datetime.now(timezone.utc)
    out = {"waiting": 0, "onway": 0, "done": 0, "late": 0, "oldest": 0}
    for st, at, voted in db.execute("SELECT status, requested_at, national_id IN (SELECT national_id FROM war_voted)"
                                    f" FROM war_transport WHERE status != 'cancelled'{mine}", list(extra)):
        if st == "done" or voted:
            out["done"] += 1
            continue
        out[st] += 1
        if st == "waiting":
            age = int((now - datetime.fromisoformat(at)).total_seconds() // 60)
            out["oldest"] = max(out["oldest"], age)
            out["late"] += age >= TRANSPORT_LATE
    return out


def transport_from_visits():
    """Our supporters in houses whose latest visit said 'needs transport'."""
    db = get_db()
    sql, sp = supporters_sql()
    if not sql:
        return 0
    rows = db.execute(
        f"SELECT s.national_id FROM ({sql}) s JOIN voters v ON v.national_id = s.national_id WHERE v.house_name IN"
        " (SELECT house_name FROM house_visits hv WHERE needs_transport = 1 AND visited_at ="
        "  (SELECT MAX(visited_at) FROM house_visits x WHERE x.house_name = hv.house_name))", sp).fetchall()
    n = sum(1 for (nid,) in rows if request_transport(nid, "visit"))
    audit.log("war_transport_visits", detail={"n": n}, commit=False)
    return n


@bp.route("/war/transport")
@login_required
def transport():
    """Tier 2 and the War Room: who needs a ride, who is on the way."""
    t = tier()
    if t not in ("t2", "owner", "room"):
        abort(403)
    if t == "t2" and not is_open():
        return _closed()
    db = get_db()
    rows = db.execute(
        "SELECT t.*, v.full_name, v.house_name, v.location, v.phone, v.photo_path, v.national_id AS nid,"
        " u.username AS runner, r.username AS requester FROM war_transport t JOIN voters v ON v.national_id = t.national_id"
        " LEFT JOIN users u ON u.id = t.runner_id LEFT JOIN users r ON r.id = t.requested_by"
        " WHERE t.status IN ('waiting', 'onway') AND t.national_id NOT IN (SELECT national_id FROM war_voted)"
        " ORDER BY t.status = 'onway', t.requested_at").fetchall()
    now = datetime.now(timezone.utc)
    ages = {r["national_id"]: int((now - datetime.fromisoformat(r["requested_at"])).total_seconds() // 60) for r in rows}
    return render_template("warroom/transport.html", rows=rows, ages=ages, t=t, me=g.user["id"], labels=TRANSPORT,
                           late=TRANSPORT_LATE, cursor=_cursor(), trev=_transport_rev(), hm=_hm)


@bp.post("/war/transport")
@login_required
def transport_action():
    t = _need("t2")
    db = get_db()
    nid = request.form.get("national_id", "")
    action = request.form.get("action", "")
    if action == "request":
        if t == "t2":
            cond, params = _scope_sql("t2", g.user["id"], "")
            if not db.execute(f"SELECT 1 FROM voters WHERE national_id = ? AND {cond}", [nid] + params).fetchone():
                abort(403)
        elif not db.execute("SELECT 1 FROM voters WHERE national_id = ?", (nid,)).fetchone():
            abort(404)
        request_transport(nid, "field" if t == "t2" else "room")
    else:
        row = db.execute("SELECT * FROM war_transport WHERE national_id = ?", (nid,)).fetchone() or abort(404)
        if action == "take" and row["status"] == "waiting":
            db.execute("UPDATE war_transport SET status = 'onway', runner_id = ?, updated_at = ? WHERE national_id = ?",
                       (g.user["id"], now_iso(), nid))
        elif action in ("release", "done", "cancel") and row["status"] in ("waiting", "onway"):
            if t == "t2" and row["runner_id"] not in (None, g.user["id"]):
                return jsonify({"ok": False, "error": "Another runner has this one."}), 409
            status = {"release": "waiting", "done": "done", "cancel": "cancelled"}[action]
            db.execute("UPDATE war_transport SET status = ?, runner_id = ?, updated_at = ? WHERE national_id = ?",
                       (status, None if action == "release" else row["runner_id"], now_iso(), nid))
        else:
            return jsonify({"ok": False, "error": "Someone else changed this already. Pull to refresh."}), 409
    audit.log("war_transport", target=nid, detail={"action": action}, commit=False)
    db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("warroom.transport"))


# --- allocation sheets (owner) -------------------------------------------------------------------

def sheet_rows(user):
    """What one agent works with on the day, for the paper backup."""
    db = get_db()
    t = user["war_tier"]
    if t == "t1":
        return db.execute(
            "SELECT l.list_no AS no, v.full_name AS name, v.national_id AS id, v.house_name AS house, '' AS phone,"
            " '' AS kind FROM voters v LEFT JOIN war_list l ON l.national_id = v.national_id"
            " WHERE v.location = ? AND v.deceased = 0 ORDER BY l.list_no IS NULL, l.list_no, v.full_name",
            (user["war_box"],)).fetchall()
    sql, sp = supporters_sql()
    if not sql:
        return []
    if t == "t2":
        cond, params = _scope_sql("t2", user["id"], "")
        where, extra = f"v.national_id IN (SELECT national_id FROM voters WHERE {cond})", params
    elif t == "t3":
        where, extra = "v.national_id IN (SELECT national_id FROM war_calls WHERE user_id = ?)", [user["id"]]
    else:
        return []
    rows = db.execute(f"SELECT NULL AS no, v.full_name AS name, v.national_id AS id, v.house_name AS house, v.phone,"
                      f" s.priority AS kind, v.location AS box FROM ({sql}) s JOIN voters v ON v.national_id = s.national_id"
                      f" WHERE {where} ORDER BY s.priority, v.house_name COLLATE NOCASE, v.full_name", sp + extra).fetchall()
    return rows


@bp.route("/war/sheets")
@login_required
def sheets():
    """Printable sheets, one per agent (or ?u=<id> for one), watermarked with the agent's name."""
    if not is_owner():
        abort(403)
    db = get_db()
    uid = request.args.get("u", type=int)
    users = db.execute("SELECT * FROM users WHERE war_tier IN ('t1', 't2', 't3') AND deleted_at IS NULL"
                       + (" AND id = ?" if uid else "") + " ORDER BY war_tier, username COLLATE NOCASE",
                       ((uid,) if uid else ())).fetchall()
    packs = [{"u": u, "rows": sheet_rows(u)} for u in users]
    audit.log("war_sheets_print", detail={"agents": len(packs)})
    return render_template("warroom/sheets.html", packs=packs, tiers=TIERS, priority=PRIORITY,
                           printed=(datetime.now(timezone.utc) + LOCAL).strftime("%-d %b %Y %H:%M"),
                           by=g.user["username"])


@bp.route("/war/sheets.xlsx")
@login_required
def sheets_xlsx():
    if not is_owner():
        abort(403)
    from openpyxl import Workbook
    from openpyxl.styles import Font

    from flask import Response

    from .voters import _safe_cell

    db = get_db()
    wb = Workbook()
    wb.remove(wb.active)
    stamp = (datetime.now(timezone.utc) + LOCAL).strftime("%-d %b %Y %H:%M")
    users = db.execute("SELECT * FROM users WHERE war_tier IN ('t1', 't2', 't3') AND deleted_at IS NULL"
                       " ORDER BY war_tier, username COLLATE NOCASE").fetchall()
    for u in users:
        ws = wb.create_sheet(re.sub(r"[\\/*?:\[\]]", "", u["username"])[:31] or f"user{u['id']}")
        ws.append([f"{TIERS[u['war_tier']]}{' · ' + u['war_box'] if u['war_box'] else ''} · for {u['username']}"
                   f" · printed {stamp} · confidential, return after the election"])
        ws["A1"].font = Font(bold=True)
        ws.append(["No.", "Name", "ID", "House", "Phone", "Group", "Done"])
        for c in ws[2]:
            c.font = Font(bold=True)
        for r in sheet_rows(u):
            ws.append([r["no"], _safe_cell(r["name"]), r["id"], _safe_cell(r["house"]),
                       "" if u["war_tier"] == "t1" else r["phone"], PRIORITY.get(r["kind"], "") if r["kind"] else "", ""])
    if not users:
        wb.create_sheet("No agents")
    out = io.BytesIO()
    wb.save(out)
    audit.log("war_sheets_export", detail={"agents": len(users)})
    return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="war-room-sheets-{datetime.now().strftime("%Y%m%d-%H%M")}.xlsx"'})


# --- live projection -----------------------------------------------------------------------------

def projection(scope, extra):
    """Seats now (from the pledges of who has voted so far) and if every pledged voter still votes."""
    from . import superpledge

    ours = pledges.our_candidate()
    cands = superpledge._by_ballot(pledges.candidates())
    if not cands:
        return None
    data = superpledge._data(f" WHERE deceased = 0{scope}", extra)
    voted = {r[0] for r in get_db().execute("SELECT national_id FROM war_voted")}
    so_far = dict(data, voters=[n for n in data["voters"] if n in voted])
    limit, seats = pledges.max_votes(), superpledge.seats()
    now = superpledge.project(so_far, {"sv": "decision", "nsr": 100}, cands, ours, limit, seats)
    final = superpledge.project(data, {"sv": "decision"}, cands, ours, limit, seats)
    return {"now": now, "final": final, "label": pledges.label, "ours": ours, "seats": seats}


# --- contact before the vote ------------------------------------------------------------------------

def set_contact(nid, status, note="", by=None):
    get_db().execute("INSERT INTO war_contact (national_id, status, at, by_user, note) VALUES (?, ?, ?, ?, ?)"
                     " ON CONFLICT(national_id) DO UPDATE SET status = excluded.status, at = excluded.at,"
                     " by_user = excluded.by_user, note = excluded.note",
                     (nid, status, now_iso(), by if by is not None else g.user["id"], note[:300]))


@bp.post("/war/contact")
@login_required
def contact():
    """Tier 2: what the voter said when the agent reached them. 'Needs transport' joins the transport queue."""
    t = _need("t2")
    db = get_db()
    nid = request.form.get("national_id", "")
    status = request.form.get("status", "")
    if status not in CONTACT and status != "":
        abort(400)
    if t == "t2":
        cond, params = _scope_sql("t2", g.user["id"], "")
        if not db.execute(f"SELECT 1 FROM voters WHERE national_id = ? AND {cond}", [nid] + params).fetchone():
            audit.log("access_denied", target=nid, detail={"war_contact": t})
            abort(403)
    elif not db.execute("SELECT 1 FROM voters WHERE national_id = ?", (nid,)).fetchone():
        abort(404)
    if status:
        set_contact(nid, status, clean_text(request.form.get("note", "")))
        if status == "transport":
            request_transport(nid, "field")
    else:
        db.execute("DELETE FROM war_contact WHERE national_id = ?", (nid,))
    audit.log("war_contact", target=nid, detail={"status": status}, commit=False)
    db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "status": status, "label": CONTACT.get(status, "Not contacted yet")})
    return redirect(request.referrer or url_for("warroom.field"))


def contact_counts(mine="", extra=()):
    """Our supporters who haven't voted, by what they told us (and how many nobody has reached)."""
    sql, sp = supporters_sql()
    if not sql:
        return {}
    rows = get_db().execute(
        f"SELECT COALESCE(c.status, '') AS st, COUNT(*) FROM ({sql}) s LEFT JOIN war_contact c ON c.national_id = s.national_id"
        f" WHERE s.national_id NOT IN (SELECT national_id FROM war_voted){mine.replace('national_id IN', 's.national_id IN', 1)}"
        " GROUP BY st", sp + list(extra)).fetchall()
    return dict(rows)


# --- private links ---------------------------------------------------------------------------------

def make_link(user, days):
    import secrets

    db = get_db()
    db.execute("UPDATE war_links SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now_iso(), user["id"]))
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    db.execute("INSERT INTO war_links (user_id, token, created_at, created_by, expires_at) VALUES (?, ?, ?, ?, ?)",
               (user["id"], secrets.token_urlsafe(24), now_iso(), g.user["id"], expires))
    audit.log("war_link_make", target=user["username"], detail={"days": days}, commit=False)


def link_user(link_id, user_id):
    """The user a War Room link session belongs to, while the link and their field tier are live."""
    db = get_db()
    row = db.execute("SELECT u.* FROM war_links l JOIN users u ON u.id = l.user_id WHERE l.id = ? AND l.user_id = ?"
                     " AND l.revoked_at IS NULL AND l.expires_at > ? AND u.deleted_at IS NULL AND u.is_active = 1"
                     " AND u.role != 'admin' AND u.war_tier IN ('t1', 't2', 't3')",
                     (link_id, user_id, now_iso())).fetchone()
    return row


@bp.route("/w/<token>")
def link_open(token):
    """Open the agent's War Room page from their private link (no password)."""
    from flask import session

    db = get_db()
    row = db.execute("SELECT * FROM war_links WHERE token = ?", (token[:64],)).fetchone()
    user = link_user(row["id"], row["user_id"]) if row else None
    if user is None:
        audit.log("war_link_refused", target=token[:6])
        db.commit()
        return render_template("warroom/link_stopped.html"), 410
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["wlink"] = row["id"]
    db.execute("UPDATE war_links SET opens = opens + 1, last_open_at = ? WHERE id = ?", (now_iso(), row["id"]))
    audit.log("war_link_open", user=user, detail={"opens": row["opens"] + 1}, commit=False)
    db.commit()
    return redirect(url_for("warroom.home"))


def _local_day(stamp):
    try:
        return (datetime.fromisoformat(stamp) + LOCAL).strftime("%-d %b %H:%M")
    except (TypeError, ValueError):
        return ""
