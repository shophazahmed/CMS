"""Candidate pledge sheet: which voters pledged to which candidates, and how the vote splits.

Admins set up the candidates (e.g. Shoppe, Rayya, Jade, Thalaal). Each voter can be marked Yes,
Maybe or No for each candidate (blank = not asked). The sheet looks like the campaign's Excel sheet
and exports in the same layout; an existing sheet can be imported (voters by ID, candidates by column
name). The distribution page shows how pledges split: per candidate, overlap, exclusive supporters,
by box, age and sex, and per week. Access is given by the owner, like the 100% confirmed list: view, or view and
mark; only the owner manages candidates, imports and downloads. The 100% confirmed column follows the
confirmed-list access.
"""
import csv
import io
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from . import audit, campaign
from .auth import (can_mark_pledges, is_owner, login_required, pledge_marker_required, pledge_owner_required,
                   pledge_viewer_required)
from .db import get_db, now_iso
from .normalize import clean_text

bp = Blueprint("pledges", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color      TEXT NOT NULL DEFAULT '#2a78d6',
    position   INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
-- No foreign key to voters, like voter_assignments: imports rewrite voter rows.
CREATE TABLE IF NOT EXISTS pledges (
    national_id  TEXT NOT NULL,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    value        TEXT NOT NULL CHECK (value IN ('yes', 'maybe', 'no')),
    at           TEXT NOT NULL,
    by_user      INTEGER,
    PRIMARY KEY (national_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS ix_pledges_candidate ON pledges (candidate_id, value);
-- Voters who won't be able to vote (abroad, sick, ...): their pledges are kept but not expected.
-- Every mark anyone makes, kept apart from the pledges themselves (which only hold the latest mark):
-- who did what and when, for the "Pledge sheet users" panel. value: yes/maybe/no/'' (cleared)/noshow/noshow_off.
CREATE TABLE IF NOT EXISTS pledge_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    national_id  TEXT NOT NULL,
    candidate_id INTEGER,
    value        TEXT NOT NULL,
    at           TEXT NOT NULL,
    by_user      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_pledge_log_user ON pledge_log (by_user, at);
CREATE TABLE IF NOT EXISTS pledge_noshow (
    national_id TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    by_user     INTEGER
);
CREATE TABLE IF NOT EXISTS voter_connector (
    national_id TEXT PRIMARY KEY,
    connector   TEXT NOT NULL,
    at          TEXT NOT NULL,
    by_user     INTEGER
);
"""

VALUES = {"yes": "Yes", "maybe": "Maybe", "no": "No"}
CYCLE = ["", "yes", "maybe", "no"]
COLORS = ["#2a78d6", "#1baf7a", "#eb6834", "#7c3aed", "#c0398a", "#e0a212", "#0f766e", "#b91c1c"]
PAGE_SIZES = (25, 50, 100, 200)
AGE_GROUPS = [("18–30", 0, 30), ("31–45", 31, 45), ("46–60", 46, 60), ("61+", 61, 200)]
MAX_IMPORT_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_VOTES = 2  # how many of these candidates one voter can vote for (0 = no limit)


def init_schema(conn):
    fresh = not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'pledge_log'").fetchone()
    conn.executescript(SCHEMA)
    if "ballot_no" not in {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}:
        try:
            conn.execute("ALTER TABLE candidates ADD COLUMN ballot_no INTEGER")  # the number on the ballot paper
        except Exception as exc:  # another worker added it first
            if "duplicate column" not in str(exc):
                raise
    if fresh:  # start the log from the marks made before it existed
        conn.execute("INSERT INTO pledge_log (national_id, candidate_id, value, at, by_user)"
                     " SELECT national_id, candidate_id, value, at, by_user FROM pledges")
        conn.execute("INSERT INTO pledge_log (national_id, candidate_id, value, at, by_user)"
                     " SELECT national_id, NULL, 'noshow', at, by_user FROM pledge_noshow")
    conn.commit()


def candidates(active_only=True):
    cond = " WHERE active = 1" if active_only else ""
    return get_db().execute(f"SELECT * FROM candidates{cond} ORDER BY position, name COLLATE NOCASE").fetchall()


def agent_options():
    """Agents for the Agent filter (none for pledge-only accounts)."""
    from .auth import is_pledge_only

    if is_pledge_only():
        return []
    return get_db().execute("SELECT id, username FROM users WHERE role IN ('agent', 'user', 'admin') AND deleted_at IS NULL"
                            " ORDER BY username COLLATE NOCASE").fetchall()


def connector_options():
    """Names for the Connector filter: typed connectors and everyone who marked a pledge (none for pledge-only)."""
    from .auth import is_pledge_only

    if is_pledge_only():
        return []
    rows = get_db().execute(
        "SELECT connector FROM voter_connector UNION SELECT u.username FROM users u WHERE u.id IN"
        " (SELECT DISTINCT by_user FROM pledges WHERE by_user IS NOT NULL)").fetchall()
    seen = {}
    for (name,) in rows:
        seen.setdefault(name.casefold(), name)
    return sorted(seen.values(), key=str.casefold)


def pattern_condition(pp, cands):
    """SQL for ?pp=only:<id> (Yes for that candidate and nobody else) or both:<a>-<b> (Yes for both, as on the
    distribution's shared supporters); (None, []) when it doesn't apply."""
    active = [c["id"] for c in cands]
    m = re.fullmatch(r"(only):(\d+)|(both):(\d+)-(\d+)", pp or "")
    if not m or not active:
        return None, []
    if m.group(1) and int(m.group(2)) in active:
        return ("national_id IN (SELECT national_id FROM pledges WHERE value = 'yes' AND candidate_id IN"
                f" ({','.join('?' * len(active))}) GROUP BY national_id HAVING COUNT(*) = 1 AND MAX(candidate_id) = ?)",
                active + [int(m.group(2))])
    if m.group(3) and {int(m.group(4)), int(m.group(5))} <= set(active) and m.group(4) != m.group(5):
        return ("national_id IN (SELECT national_id FROM pledges WHERE value = 'yes' AND candidate_id IN (?, ?)"
                " GROUP BY national_id HAVING COUNT(*) = 2)", [int(m.group(4)), int(m.group(5))])
    return None, []


def undecided_counts(where, params):
    """(leaning, undecided) among the voters of a WHERE clause, no-shows left out: leaning = a Maybe for
    someone but no Yes; undecided = no mark at all."""
    row = get_db().execute(
        "SELECT SUM(m.nid IS NOT NULL AND y.nid IS NULL), SUM(a.nid IS NULL) FROM voters v"
        " LEFT JOIN (SELECT DISTINCT national_id AS nid FROM pledges WHERE value = 'maybe') m ON m.nid = v.national_id"
        " LEFT JOIN (SELECT DISTINCT national_id AS nid FROM pledges WHERE value = 'yes') y ON y.nid = v.national_id"
        " LEFT JOIN (SELECT DISTINCT national_id AS nid FROM pledges) a ON a.nid = v.national_id"
        f" WHERE v.national_id IN (SELECT national_id FROM voters{where})"
        " AND v.national_id NOT IN (SELECT national_id FROM pledge_noshow)", params).fetchone()
    return (row[0] or 0, row[1] or 0)


def our_candidate():
    """The campaign's own candidate (this is their portal), as a candidate id, or None."""
    value = campaign.get_setting("our_candidate")
    return int(value) if value and str(value).isdigit() else None


def label(c):
    """'#2 Sofhaz' when the ballot number is set."""
    return f"#{c['ballot_no']} {c['name']}" if c["ballot_no"] else c["name"]


def max_votes():
    """How many candidates one voter can vote for (0 = no limit)."""
    value = campaign.get_setting("pledge_max")
    if value is None:  # the earlier one / several setting
        old = campaign.get_setting("pledge_mode")
        return 1 if old == "one" else DEFAULT_MAX_VOTES
    return int(value) if str(value).isdigit() else DEFAULT_MAX_VOTES


def noshow_ids(ids):
    out = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out |= {r[0] for r in get_db().execute(
            f"SELECT national_id FROM pledge_noshow WHERE national_id IN ({','.join('?' * len(chunk))})", chunk)}
    return out


RIDES = {"waiting": "Needs ride", "onway": "On the way", "done": "Dropped"}


def transport_map(ids):
    """{national_id: 'waiting' / 'onway' / 'done'}: these voters' place in the War Room transport queue."""
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update(get_db().execute(
            f"SELECT national_id, status FROM war_transport WHERE status IN ('waiting', 'onway', 'done')"
            f" AND national_id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    return out


def voted_ids(ids):
    """Of these voters, those the War Room has marked voted."""
    out = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out |= {r[0] for r in get_db().execute(
            f"SELECT national_id FROM war_voted WHERE national_id IN ({','.join('?' * len(chunk))})", chunk)}
    return out


# --- the sheet ----------------------------------------------------------------------------

def _filters():
    """The Search filters (search, house, box, party, sex, age, agent…) plus pledge filters."""
    from .voters import build_where, read_filters

    f = read_filters(request.args)
    where, params = build_where(f)
    cands = candidates()
    ids = {str(c["id"]) for c in cands}
    pc = request.args.get("pc", "")
    pc = pc if pc in ids or pc == "any" else ""
    pv = request.args.get("pv", "")
    pv = pv if pv in ("yes", "maybe", "no", "none", "leaning", "undecided") else ""
    if pv in ("leaning", "undecided"):
        pc = "any"
        cond = ("national_id NOT IN (SELECT national_id FROM pledge_noshow) AND national_id NOT IN"
                " (SELECT national_id FROM pledges WHERE value = 'yes') AND national_id")
        cond += (" IN (SELECT national_id FROM pledges WHERE value = 'maybe')" if pv == "leaning"
                 else " NOT IN (SELECT national_id FROM pledges)")
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
    elif pc and pv:
        if pc == "any":
            # "not pledged" = no Yes for anyone (a Maybe or a No doesn't count as a pledge)
            cond = ("national_id NOT IN (SELECT national_id FROM pledges WHERE value = 'yes')" if pv == "none" else
                    "national_id IN (SELECT national_id FROM pledges WHERE value = ?)")
            extra = [] if pv == "none" else [pv]
        elif pv == "none":
            cond, extra = "national_id NOT IN (SELECT national_id FROM pledges WHERE candidate_id = ?)", [int(pc)]
        else:
            cond, extra = ("national_id IN (SELECT national_id FROM pledges WHERE candidate_id = ? AND value = ?)",
                           [int(pc), pv])
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
        params = list(params) + extra
    ns = request.args.get("ns", "")
    if ns in ("1", "0"):
        cond = ("national_id IN (SELECT national_id FROM pledge_noshow)" if ns == "1"
                else "national_id NOT IN (SELECT national_id FROM pledge_noshow)")
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
    f["ns"] = ns if ns in ("1", "0") else ""
    # ?pp=only:<id> (Yes for that candidate and nobody else) or both:<a>-<b> (Yes for both, as on the distribution)
    pp, f["pp"] = request.args.get("pp", ""), ""
    cond, extra = pattern_condition(pp, cands)
    if cond:
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
        params = list(params) + extra
        f["pp"] = pp
    # ?ag=<user id> / none: the responsible agent (one by one, else the house's agent). Not for pledge-only accounts.
    from .auth import is_pledge_only
    from .voters import handled_by

    ag, f["ag"] = request.args.get("ag", ""), ""
    if (ag == "none" or ag.isdigit()) and not is_pledge_only():
        cond = ("(national_id NOT IN (SELECT national_id FROM voter_assignments) AND house_name NOT IN"
                " (SELECT house_name FROM house_assignments))") if ag == "none" else handled_by()
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
        params = list(params) + ([] if ag == "none" else [int(ag), int(ag)])
        f["ag"] = ag
    # ?cn=<name> / none: the voter's connector (the typed one, else who marked their pledges). Not for pledge-only.
    cn, f["cn"] = " ".join(request.args.get("cn", "").split())[:80], ""
    if cn and not is_pledge_only():
        if cn == "none":
            cond = ("national_id NOT IN (SELECT national_id FROM voter_connector)"
                    " AND national_id NOT IN (SELECT national_id FROM pledges WHERE by_user IS NOT NULL)")
            extra = []
        else:
            cond = ("(national_id IN (SELECT national_id FROM voter_connector WHERE connector = ? COLLATE NOCASE)"
                    " OR (national_id NOT IN (SELECT national_id FROM voter_connector) AND national_id IN"
                    " (SELECT p.national_id FROM pledges p JOIN users u ON u.id = p.by_user WHERE u.username = ? COLLATE NOCASE)))")
            extra = [cn, cn]
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
        params = list(params) + extra
        f["cn"] = cn
    # ?sp=<candidate id> / any / none: super pledge voters (for that candidate). Needs super pledge access.
    from .auth import can_view_super

    sp, f["sp"] = request.args.get("sp", ""), ""
    if can_view_super() and (sp in ("any", "none") or sp in {str(c["id"]) for c in cands}):
        cond = {"any": "national_id IN (SELECT national_id FROM super_votes)",
                "none": "national_id NOT IN (SELECT national_id FROM super_votes)"}.get(
            sp, "national_id IN (SELECT national_id FROM super_votes WHERE candidate_id = ?)")
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
        params = list(params) + ([int(sp)] if sp.isdigit() else [])
        f["sp"] = sp
    pb = request.args.get("pb", "")
    f["pb"], f["pb_name"] = "", ""
    if pb.isdigit() and g.user["role"] == "admin":
        row = get_db().execute("SELECT username FROM users WHERE id = ?", (int(pb),)).fetchone()
        if row:
            cond = "national_id IN (SELECT national_id FROM pledge_log WHERE by_user = ?)"
            where = f"{where} AND {cond}" if where else f" WHERE {cond}"
            params = list(params) + [int(pb)]
            f["pb"], f["pb_name"] = pb, row[0]
    return f, where, params, cands, pc, pv


# Sortable columns on the sheet: (A to Z, Z to A); empty values always go last
_LISTNO = "(SELECT list_no FROM war_list WHERE war_list.national_id = voters.national_id)"


def list_numbers(ids):
    """{national_id: official list number} for these voters."""
    out = {}
    db = get_db()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update(db.execute(f"SELECT national_id, list_no FROM war_list WHERE national_id IN ({','.join('?' * len(chunk))})",
                              chunk).fetchall())
    return out


SHEET_SORTS = {
    "name": ("full_name COLLATE NOCASE, national_id", "full_name COLLATE NOCASE DESC, national_id"),
    "house": ("house_name = '', house_name COLLATE NOCASE, full_name COLLATE NOCASE",
              "house_name = '', house_name COLLATE NOCASE DESC, full_name COLLATE NOCASE"),
    "box": ("location = '', location COLLATE NOCASE, house_name COLLATE NOCASE, full_name COLLATE NOCASE",
            "location = '', location COLLATE NOCASE DESC, house_name COLLATE NOCASE, full_name COLLATE NOCASE"),
    "age": ("age IS NULL, age, full_name COLLATE NOCASE", "age IS NULL, age DESC, full_name COLLATE NOCASE"),
    "id": ("national_id", "national_id DESC"),
    # the official list number, box by box (voters without one last)
    "no": ("location = '', location COLLATE NOCASE, " + _LISTNO + " IS NULL, " + _LISTNO + ", full_name COLLATE NOCASE",
           "location = '', location COLLATE NOCASE, " + _LISTNO + " IS NULL, " + _LISTNO + " DESC, full_name COLLATE NOCASE"),
}


def sheet_order():
    """(key, direction, ORDER BY) from ?sort=name|house|box|age|id&dir=asc|desc (default: name, A to Z)."""
    key = request.args.get("sort", "name")
    key = key if key in SHEET_SORTS else "name"
    direction = "desc" if request.args.get("dir") == "desc" else "asc"
    return key, direction, SHEET_SORTS[key][1 if direction == "desc" else 0]


def connected_by(ids, table="pledges"):
    """{national_id: "name, name"}: who marked these voters' pledges (or super votes), latest first."""
    out = {}
    db = get_db()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for nid, name in db.execute(
                f"SELECT t.national_id, u.username FROM {table} t JOIN users u ON u.id = t.by_user"
                f" WHERE t.national_id IN ({','.join('?' * len(chunk))}) ORDER BY t.at DESC, t.rowid DESC", chunk):
            names = out.setdefault(nid, [])
            if name not in names:
                names.append(name)
    return {k: ", ".join(v) for k, v in out.items()}


def agents_of(ids):
    """{national_id: agent's name}: who looks after these voters (one by one, else the house's agent)."""
    out = {}
    db = get_db()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update(db.execute(
            "SELECT v.national_id, COALESCE(ua.username, uh.username) FROM voters v"
            " LEFT JOIN voter_assignments va ON va.national_id = v.national_id"
            " LEFT JOIN house_assignments ha ON ha.house_name = v.house_name"
            " LEFT JOIN users ua ON ua.id = va.user_id LEFT JOIN users uh ON uh.id = ha.user_id"
            f" WHERE v.national_id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
    return {k: v for k, v in out.items() if v}


def connectors(ids):
    """{national_id: connector}: the person typed into the sheets' Connector field."""
    out = {}
    db = get_db()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        out.update(db.execute(f"SELECT national_id, connector FROM voter_connector WHERE national_id IN"
                              f" ({','.join('?' * len(chunk))})", chunk).fetchall())
    return out


def connector_or_auto(ids, table="pledges"):
    """{national_id: connector}: the typed connector, else who marked the voter (for exports)."""
    out = connected_by(ids, table)
    out.update(connectors(ids))
    return out


def agent_cells(ids):
    """{national_id: (one-by-one agent id or None, house agent's name or '')} for the Agent drop-down."""
    out = {}
    db = get_db()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for nid, one, house in db.execute(
                "SELECT v.national_id, va.user_id, uh.username FROM voters v"
                " LEFT JOIN voter_assignments va ON va.national_id = v.national_id"
                " LEFT JOIN house_assignments ha ON ha.house_name = v.house_name"
                " LEFT JOIN users uh ON uh.id = ha.user_id AND uh.deleted_at IS NULL"
                f" WHERE v.national_id IN ({','.join('?' * len(chunk))})", chunk):
            out[nid] = (one, house or "")
    return out


def can_set_connector():
    from .auth import can_mark_super, is_pledge_only

    return g.get("user") is not None and not is_pledge_only() and (can_mark_pledges() or can_mark_super())


def sheet_extras(ids, table="pledges"):
    """What the Connector and Agent columns need on the pledge and super sheets."""
    from .auth import is_admin

    return {"connected": connected_by(ids, table), "connectors": connectors(ids), "agents": agents_of(ids),
            "agent_cells": agent_cells(ids), "agent_choices": agent_options() if is_admin() else [],
            "can_connect": can_set_connector()}


def _pledge_map(ids):
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in get_db().execute(
                f"SELECT national_id, candidate_id, value FROM pledges WHERE national_id IN ({','.join('?' * len(chunk))})",
                chunk):
            out.setdefault(r["national_id"], {})[r["candidate_id"]] = r["value"]
    return out


def _totals(where, params, cands):
    rows = get_db().execute(
        f"SELECT candidate_id, value, COUNT(*) FROM pledges WHERE national_id IN (SELECT national_id FROM voters{where})"
        " GROUP BY candidate_id, value", params).fetchall()
    t = {c["id"]: {"yes": 0, "maybe": 0, "no": 0} for c in cands}
    for cid, value, n in rows:
        if cid in t:
            t[cid][value] = n
    return t


def can_view_super_pages():
    from .auth import can_view_super

    return can_view_super()


@bp.route("/pledges")
@pledge_viewer_required
def sheet():
    f, where, params, cands, pc, pv = _filters()
    sort_key, sort_dir, order = sheet_order()
    db = get_db()
    size = request.args.get("n", type=int)
    size = size if size in PAGE_SIZES else 50
    total = db.execute(f"SELECT COUNT(*) FROM voters{where}", params).fetchone()[0]
    pages = max(1, -(-total // size))
    try:
        page = min(max(1, int(request.args.get("page", 1))), pages)
    except ValueError:
        page = 1
    rows = db.execute(f"SELECT * FROM voters{where} ORDER BY {order} LIMIT ? OFFSET ?",
                      params + [size, (page - 1) * size]).fetchall()
    w, p = _scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    query_args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("pledges/sheet.html", rows=rows, pledges=_pledge_map([r["national_id"] for r in rows]),
                           cands=cands, totals=_totals(where, params, cands), total=total, page=page, pages=pages,
                           size=size, sizes=PAGE_SIZES, f=f, pc=pc, pv=pv, values=VALUES, boxes=boxes,
                           can_mark=can_mark_pledges(), owner=is_owner(), max_votes=max_votes(),
                           sort_key=sort_key, sort_dir=sort_dir,
                           noshow=noshow_ids([r["national_id"] for r in rows]),
                           undecided=undecided_counts(where, params), show_super=can_view_super_pages(),
                           listno=list_numbers([r["national_id"] for r in rows]),
                           **sheet_extras([r["national_id"] for r in rows]),
                           rides=transport_map([r["national_id"] for r in rows]), ride_labels=RIDES,
                           voted=voted_ids([r["national_id"] for r in rows]),
                           noshows=db.execute(f"SELECT COUNT(*) FROM pledge_noshow WHERE national_id IN"
                                              f" (SELECT national_id FROM voters{where})", params).fetchone()[0],
                           query_args=query_args, first=(page - 1) * size)


def _scoped_where(sql="", params=()):
    from .voters import scoped

    return scoped(sql, params)


def _log(nid, cid, value):
    get_db().execute("INSERT INTO pledge_log (national_id, candidate_id, value, at, by_user) VALUES (?, ?, ?, ?, ?)",
                     (nid, cid, value, now_iso(), g.user["id"]))


def user_activity(rng):
    """The pledge-sheet-only accounts and what each marked (in the date range when one is given)."""
    db = get_db()
    users = db.execute("SELECT id, username, is_active, last_login_at, created_at FROM users"
                       " WHERE pledge_only = 1 AND deleted_at IS NULL ORDER BY username COLLATE NOCASE").fetchall()
    if not users:
        return []
    cond, params = ["by_user IN (%s)" % ",".join("?" * len(users))], [u["id"] for u in users]
    if rng.get("start_utc"):
        cond.append("at >= ?")
        params.append(rng["start_utc"])
    if rng.get("end_utc"):
        cond.append("at < ?")
        params.append(rng["end_utc"])
    w = " WHERE " + " AND ".join(cond)
    sums = {r["by_user"]: r for r in db.execute(
        "SELECT by_user, COUNT(*) AS marks, COUNT(DISTINCT national_id) AS voters, SUM(value = 'yes') AS yes,"
        " SUM(value = 'maybe') AS maybe, SUM(value = 'no') AS no, SUM(value = '') AS cleared,"
        f" SUM(value = 'noshow') AS noshow, MAX(at) AS last_at FROM pledge_log{w} GROUP BY by_user", params)}
    per_cand = {}
    for r in db.execute(f"SELECT by_user, candidate_id, COUNT(DISTINCT national_id) FROM pledge_log{w}"
                        " AND value = 'yes' GROUP BY by_user, candidate_id", params):
        per_cand.setdefault(r[0], {})[r[1]] = r[2]
    # Yes marks that are still theirs now (nobody changed them since)
    held = dict(db.execute("SELECT by_user, COUNT(*) FROM pledges WHERE value = 'yes' AND by_user IN (%s)"
                           " GROUP BY by_user" % ",".join("?" * len(users)), [u["id"] for u in users]).fetchall())
    out = []
    for u in users:
        s = sums.get(u["id"])
        out.append({"id": u["id"], "username": u["username"], "active": u["is_active"], "last_login": u["last_login_at"],
                    "marks": s["marks"] if s else 0, "voters": s["voters"] if s else 0,
                    "yes": (s["yes"] or 0) if s else 0, "maybe": (s["maybe"] or 0) if s else 0,
                    "no": (s["no"] or 0) if s else 0, "cleared": (s["cleared"] or 0) if s else 0,
                    "noshow": (s["noshow"] or 0) if s else 0, "last_at": s["last_at"] if s else None,
                    "cands": per_cand.get(u["id"], {}), "held": held.get(u["id"], 0)})
    return out


@bp.post("/pledges/set")
@pledge_marker_required
def set_pledge():
    from .voters import get_voter_or_404

    nid = request.form.get("national_id", "")
    get_voter_or_404(nid)
    cid = request.form.get("candidate_id", type=int)
    if cid is None or not get_db().execute("SELECT 1 FROM candidates WHERE id = ?", (cid,)).fetchone():
        abort(400)
    value = request.form.get("value", "")
    if value not in CYCLE:
        abort(400)
    db = get_db()
    old = db.execute("SELECT value FROM pledges WHERE national_id = ? AND candidate_id = ?", (nid, cid)).fetchone()
    old = old[0] if old else ""
    limit = max_votes()
    if value == "yes" and old != "yes" and limit:
        others = db.execute("SELECT COUNT(*) FROM pledges WHERE national_id = ? AND value = 'yes' AND candidate_id != ?",
                            (nid, cid)).fetchone()[0]
        if others >= limit:
            msg = (f"A voter can vote for {limit} candidate{'s' if limit != 1 else ''}: this voter already has "
                   f"{others} Yes. Change one of them first.")
            if request.headers.get("X-Requested-With") == "fetch":
                return {"ok": False, "error": msg, "value": old, "label": VALUES.get(old, "")}, 409
            flash(msg, "error")
            return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/")
                            else url_for("pledges.sheet"))
    if value != old:
        if value:
            db.execute("INSERT INTO pledges (national_id, candidate_id, value, at, by_user) VALUES (?, ?, ?, ?, ?)"
                       " ON CONFLICT(national_id, candidate_id) DO UPDATE SET value = excluded.value, at = excluded.at,"
                       " by_user = excluded.by_user", (nid, cid, value, now_iso(), g.user["id"]))
        else:
            db.execute("DELETE FROM pledges WHERE national_id = ? AND candidate_id = ?", (nid, cid))
        _log(nid, cid, value)
        audit.log("pledge", target=nid, detail={"candidate": cid, "from": old, "to": value}, commit=False)
        db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "value": value, "label": VALUES.get(value, "")}
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/") else url_for("pledges.sheet"))


@bp.post("/pledges/noshow")
@pledge_marker_required
def set_noshow():
    """Mark (1) or unmark (0) a voter who won't be able to vote."""
    from .voters import get_voter_or_404

    nid = request.form.get("national_id", "")
    get_voter_or_404(nid)
    wanted = request.form.get("value", "")
    if wanted not in ("0", "1"):
        abort(400)
    db = get_db()
    was = db.execute("SELECT 1 FROM pledge_noshow WHERE national_id = ?", (nid,)).fetchone() is not None
    if wanted == "1" and not was:
        db.execute("INSERT INTO pledge_noshow (national_id, at, by_user) VALUES (?, ?, ?)", (nid, now_iso(), g.user["id"]))
    elif wanted == "0" and was:
        db.execute("DELETE FROM pledge_noshow WHERE national_id = ?", (nid,))
    if (wanted == "1") != was:
        _log(nid, None, "noshow" if wanted == "1" else "noshow_off")
        audit.log("pledge_noshow", target=nid, detail={"noshow": wanted == "1"}, commit=False)
        db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "noshow": wanted == "1"}
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/") else url_for("pledges.sheet"))


@bp.post("/pledges/transport")
@pledge_marker_required
def set_transport():
    """Needs transport (value=1): the voter joins the War Room transport queue; 0 takes a waiting request back."""
    from .voters import get_voter_or_404
    from .warroom import request_transport

    nid = request.form.get("national_id", "")
    get_voter_or_404(nid)
    wanted = request.form.get("value", "")
    if wanted not in ("0", "1"):
        abort(400)
    db = get_db()
    row = db.execute("SELECT status FROM war_transport WHERE national_id = ?", (nid,)).fetchone()
    now = row["status"] if row else ""
    error = ""
    if wanted == "1":
        if db.execute("SELECT 1 FROM war_voted WHERE national_id = ?", (nid,)).fetchone():
            error = "This voter has already voted."
        elif request_transport(nid, "pledge sheet"):
            now = "waiting"
    elif now == "waiting":
        db.execute("UPDATE war_transport SET status = 'cancelled', updated_at = ? WHERE national_id = ?", (now_iso(), nid))
        now = ""
    elif now == "onway":
        error = "A runner is already on the way: the War Room can cancel it on the Transport page."
    if not error:
        audit.log("pledge_transport", target=nid, detail={"wanted": wanted, "status": now}, commit=False)
        db.commit()
    now = now if now in RIDES else ""
    if request.headers.get("X-Requested-With") == "fetch":
        body = {"ok": not error, "status": now, "label": RIDES.get(now, "")}
        if error:
            body["error"] = error
        return body, (409 if error else 200)
    if error:
        flash(error, "error")
    back = request.form.get("back") or ""
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("pledges.sheet"))


@bp.post("/pledges/connector")
@login_required
def set_connector():
    """The Connector field on the pledge / super sheets: who brought this voter in (typed, saved on change)."""
    from .voters import get_voter_or_404

    if not can_set_connector():
        abort(403)
    nid = request.form.get("national_id", "")
    get_voter_or_404(nid)
    text = " ".join(request.form.get("connector", "").split())[:80]
    db = get_db()
    old = db.execute("SELECT connector FROM voter_connector WHERE national_id = ?", (nid,)).fetchone()
    old = old[0] if old else ""
    if text != old:
        if text:
            db.execute("INSERT INTO voter_connector (national_id, connector, at, by_user) VALUES (?, ?, ?, ?)"
                       " ON CONFLICT(national_id) DO UPDATE SET connector = excluded.connector, at = excluded.at,"
                       " by_user = excluded.by_user", (nid, text, now_iso(), g.user["id"]))
        else:
            db.execute("DELETE FROM voter_connector WHERE national_id = ?", (nid,))
        audit.log("voter_connector", target=nid, detail={"from": old, "to": text}, commit=False)
        db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "connector": text}
    back = request.form.get("back") or ""
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("pledges.sheet"))


@bp.route("/pledges/colors.css")
@pledge_viewer_required
def colors_css():
    """Each candidate's colour as CSS (a file, since the CSP forbids inline styles)."""
    css = "".join(f".cand-{c['id']}{{--cand:{c['color']};}}\n" for c in candidates(active_only=False))
    return Response(css, mimetype="text/css", headers={"Cache-Control": "private, no-cache"})


# --- candidates -------------------------------------------------------------------------------

@bp.route("/pledges/candidates", methods=["GET", "POST"])
@pledge_owner_required
def manage():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "add":
            name = clean_text(request.form.get("name", ""))[:60]
            if not name:
                flash("Type the candidate's name.", "error")
            elif db.execute("SELECT 1 FROM candidates WHERE name = ?", (name,)).fetchone():
                flash(f"{name} is already on the list.", "error")
            else:
                n = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
                db.execute("INSERT INTO candidates (name, color, position, created_at) VALUES (?, ?, ?, ?)",
                           (name, COLORS[n % len(COLORS)], n, now_iso()))
                audit.log("candidate_add", target=name, commit=False)
                flash(f"{name} added.", "success")
        elif action in ("rename", "up", "down", "retire", "restore"):
            cid = request.form.get("id", type=int)
            cand = db.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone() or abort(404)
            if action == "rename":
                name = clean_text(request.form.get("name", ""))[:60]
                color = request.form.get("color", cand["color"])
                color = color if re.fullmatch(r"#[0-9a-fA-F]{6}", color or "") else cand["color"]
                ballot = request.form.get("ballot_no", "").strip()
                ballot = int(ballot) if ballot.isdigit() and 0 < int(ballot) < 100 else None
                if name and not db.execute("SELECT 1 FROM candidates WHERE name = ? AND id != ?", (name, cid)).fetchone():
                    db.execute("UPDATE candidates SET name = ?, color = ?, ballot_no = ? WHERE id = ?", (name, color, ballot, cid))
                    audit.log("candidate_rename", target=name, detail={"from": cand["name"]}, commit=False)
                    flash("Saved.", "success")
                else:
                    flash("That name is empty or already used.", "error")
            elif action in ("up", "down"):
                order = [r["id"] for r in candidates(active_only=False)]
                i = order.index(cid)
                j = i - 1 if action == "up" else i + 1
                if 0 <= j < len(order):
                    order[i], order[j] = order[j], order[i]
                    db.executemany("UPDATE candidates SET position = ? WHERE id = ?", [(k, c) for k, c in enumerate(order)])
            else:
                db.execute("UPDATE candidates SET active = ? WHERE id = ?", (1 if action == "restore" else 0, cid))
                audit.log(f"candidate_{action}", target=cand["name"], commit=False)
                flash(f"{cand['name']} {'is back on the sheet' if action == 'restore' else 'hidden from the sheet (pledges kept)'}.",
                      "success")
        elif action == "ours":
            value = request.form.get("ours", "")
            if value == "" or db.execute("SELECT 1 FROM candidates WHERE id = ?", (value,)).fetchone():
                campaign.put_setting("our_candidate", value)
                audit.log("our_candidate", detail={"candidate": value}, commit=False)
                flash("Saved: our candidate.", "success")
        elif action == "max_votes":
            value = request.form.get("max_votes", "")
            if value.isdigit() and 0 <= int(value) <= 20:
                campaign.put_setting("pledge_max", value)
                audit.log("pledge_max_votes", detail={"max": int(value)}, commit=False)
                flash("No limit on how many candidates a voter can vote for." if value == "0"
                      else f"A voter can vote for {value} candidate{'s' if value != '1' else ''}.", "success")
            else:
                abort(400)
        else:
            abort(400)
        db.commit()
        return redirect(url_for("pledges.manage"))
    counts = dict(db.execute("SELECT candidate_id, COUNT(*) FROM pledges WHERE value = 'yes' GROUP BY candidate_id").fetchall())
    return render_template("pledges/candidates.html", cands=candidates(active_only=False), counts=counts,
                           max_votes=max_votes(), ours=our_candidate())


# --- export / import -----------------------------------------------------------------------

@bp.route("/pledges/export.<fmt>")
@pledge_owner_required
def export(fmt):
    """The sheet in the campaign's Excel layout, with the same filters."""
    from .voters import _safe_cell

    if fmt not in ("csv", "xlsx"):
        abort(404)
    f, where, params, cands, pc, pv = _filters()
    rows = get_db().execute(f"SELECT * FROM voters{where} ORDER BY {sheet_order()[2]}", params).fetchall()
    marks = _pledge_map([r["national_id"] for r in rows])
    away = noshow_ids([r["national_id"] for r in rows])
    listno = list_numbers([r["national_id"] for r in rows])
    connected = connector_or_auto([r["national_id"] for r in rows])
    agent = agents_of([r["national_id"] for r in rows])
    headers = (["#", "ID", "Full name", "Sex", "Age", "House name", "Party", "Phone", "Box", "List no.", "Agent",
                "Connector", "No-show", "Transport"]
               + [c["name"] for c in cands])
    rides = transport_map([r["national_id"] for r in rows])
    table = []
    for n, r in enumerate(rows, 1):
        mine = marks.get(r["national_id"], {})
        table.append([n, r["national_id"], _safe_cell(r["full_name"]), r["sex"], r["age"], _safe_cell(r["house_name"]),
                      _safe_cell(r["party"]), r["phone"], _safe_cell(r["location"]), listno.get(r["national_id"], ""),
                      agent.get(r["national_id"], ""), connected.get(r["national_id"], ""),
                      "yes" if r["national_id"] in away else "", RIDES.get(rides.get(r["national_id"]), "")]
                     + [VALUES.get(mine.get(c["id"]), "").lower() for c in cands])
    audit.log("pledges_export", target=fmt, detail={"rows": len(table)})
    name = f"pledge-sheet-{datetime.now().strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(table)
        return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Pledges"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    first = len(headers) - len(cands) + 1
    for i, c in enumerate(cands):
        ws.cell(row=1, column=first + i).fill = PatternFill("solid", fgColor=c["color"].lstrip("#"))
        ws.cell(row=1, column=first + i).font = Font(bold=True, color="FFFFFF")
    for row in table:
        ws.append(row)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), headers={"Content-Disposition": f'attachment; filename="{name}"'},
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _read_table(upload):
    """Rows (lists of strings) from an .xlsx or .csv upload."""
    blob = upload.read(MAX_IMPORT_BYTES + 1)
    if len(blob) > MAX_IMPORT_BYTES:
        raise ValueError("The file must be 20 MB or smaller.")
    name = (upload.filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        try:
            ws = load_workbook(io.BytesIO(blob), read_only=True, data_only=True).active
        except Exception:  # noqa: BLE001
            raise ValueError("That Excel file can't be read.")
        return [["" if v is None else str(v).strip() for v in row] for row in ws.iter_rows(values_only=True)]
    if name.endswith(".csv"):
        text = blob.decode("utf-8-sig", errors="replace")
        return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]
    raise ValueError("Upload an Excel (.xlsx) or CSV file.")


def _cell_value(text):
    t = (text or "").strip().lower()
    if t in ("yes", "y", "1", "true", "✓", "✔", "x", "ok"):
        return "yes"
    if t in ("maybe", "m", "?"):
        return "maybe"
    if t in ("no", "n", "0"):
        return "no"
    return None


@bp.post("/pledges/import")
@pledge_owner_required
def import_sheet():
    from .normalize import normalize_id

    upload = request.files.get("sheet")
    if not upload or not upload.filename:
        flash("Choose the pledge sheet first.", "error")
        return redirect(url_for("pledges.manage"))
    try:
        rows = _read_table(upload)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("pledges.manage"))
    cands = {c["name"].casefold(): c for c in candidates(active_only=False)}
    header_at, id_col, cols, noshow_col = None, None, {}, None
    for i, row in enumerate(rows[:15]):  # the header can be below a title row
        keys = [re.sub(r"\s+", " ", c).strip().casefold() for c in row]
        if any(k in ("id", "id card", "national id", "id no", "id number") for k in keys):
            header_at = i
            id_col = next(j for j, k in enumerate(keys) if k in ("id", "id card", "national id", "id no", "id number"))
            cols = {j: cands[k] for j, k in enumerate(keys) if k in cands}
            noshow_col = next((j for j, k in enumerate(keys) if k.replace("-", "").replace(" ", "") == "noshow"), None)
            break
    if header_at is None:
        flash("No “ID” column found in the first rows of the sheet.", "error")
        return redirect(url_for("pledges.manage"))
    if not cols and noshow_col is None:
        flash("No column is named after a candidate on the list. Add the candidates first, with the same names "
              "as the sheet's column headings.", "error")
        return redirect(url_for("pledges.manage"))
    db = get_db()
    known = {r[0] for r in db.execute("SELECT national_id FROM voters")}
    stamp, set_n, unknown, skipped, away_n = now_iso(), 0, 0, 0, 0
    for row in rows[header_at + 1:]:
        if id_col >= len(row) or not row[id_col]:
            continue
        nid, _ = normalize_id(row[id_col])
        if not nid or nid not in known:
            unknown += 1
            continue
        if noshow_col is not None and noshow_col < len(row) and _cell_value(row[noshow_col]) == "yes":
            db.execute("INSERT OR IGNORE INTO pledge_noshow (national_id, at, by_user) VALUES (?, ?, ?)",
                       (nid, stamp, g.user["id"]))
            away_n += 1
        for j, cand in cols.items():
            value = _cell_value(row[j]) if j < len(row) else None
            if value is None:
                skipped += 1 if j < len(row) and row[j] else 0
                continue
            db.execute("INSERT INTO pledges (national_id, candidate_id, value, at, by_user) VALUES (?, ?, ?, ?, ?)"
                       " ON CONFLICT(national_id, candidate_id) DO UPDATE SET value = excluded.value, at = excluded.at,"
                       " by_user = excluded.by_user", (nid, cand["id"], value, stamp, g.user["id"]))
            set_n += 1
    audit.log("pledges_import", detail={"marks": set_n, "noshows": away_n, "unknown_ids": unknown, "unreadable": skipped,
                                        "candidates": [c["name"] for c in cols.values()]}, commit=False)
    db.commit()
    msg = f"Imported {set_n} pledge mark{'s' if set_n != 1 else ''} for {', '.join(c['name'] for c in cols.values()) or 'no candidate'}."
    if away_n:
        msg += f" {away_n} no-show{'s' if away_n != 1 else ''}."
    if unknown:
        msg += f" {unknown} row{'s' if unknown != 1 else ''} had an ID that isn't in the portal."
    if skipped:
        msg += f" {skipped} cell{'s' if skipped != 1 else ''} weren't yes / maybe / no and were left out."
    flash(msg, "success")
    return redirect(url_for("pledges.manage"))


def dashboard_numbers(box="", party=""):
    """Yes pledges per candidate for the admin dashboard (campaign voters; one box / party when given)."""
    from .voters import scoped

    sql, params = " WHERE deceased = 0", []
    if box:
        sql, params = sql + " AND location = ?", params + ["" if box == "(none)" else box]
    if party:
        sql, params = sql + " AND party = ?", params + [party]
    w, p = scoped(sql, params)
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM voters{w}", p).fetchone()[0]
    rows = dict(db.execute(f"SELECT candidate_id, COUNT(*) FROM pledges WHERE value = 'yes' AND national_id IN"
                           f" (SELECT national_id FROM voters{w}) GROUP BY candidate_id", p).fetchall())
    anyone = db.execute(f"SELECT COUNT(DISTINCT national_id) FROM pledges WHERE value = 'yes' AND national_id IN"
                        f" (SELECT national_id FROM voters{w})", p).fetchone()[0]
    cands = candidates()
    away = db.execute(f"SELECT COUNT(*) FROM pledge_noshow WHERE national_id IN (SELECT national_id FROM voters{w})",
                      p).fetchone()[0]
    leaning, undecided = undecided_counts(w, p)
    return {"total": total, "not_pledged": total - anyone, "noshows": away, "leaning": leaning, "undecided": undecided,
            "cands": [{"id": c["id"], "name": c["name"], "yes": rows.get(c["id"], 0),
                       "pct": round(100 * rows.get(c["id"], 0) / total, 1) if total else 0} for c in cands]}


# --- distribution ----------------------------------------------------------------------------

@bp.route("/pledges/distribution")
@pledge_viewer_required
def distribution():
    f, where, params, cands, pc, pv = _filters()
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM voters{where}", params).fetchone()[0]
    sub = f"SELECT national_id FROM voters{where}"
    yes = db.execute(f"SELECT national_id, candidate_id FROM pledges WHERE value = 'yes' AND national_id IN ({sub})",
                     params).fetchall()
    maybe = db.execute(f"SELECT candidate_id, COUNT(*) FROM pledges WHERE value = 'maybe' AND national_id IN ({sub})"
                       " GROUP BY candidate_id", params).fetchall()
    ids = [c["id"] for c in cands]
    by_voter = {}
    for nid, cid in yes:
        if cid in ids:
            by_voter.setdefault(nid, set()).add(cid)
    any_mark = db.execute(f"SELECT COUNT(DISTINCT national_id) FROM pledges WHERE national_id IN ({sub})"
                          f" AND candidate_id IN ({','.join('?' * len(ids)) or 'NULL'})", params + ids).fetchone()[0]
    yes_count = {c: 0 for c in ids}
    exclusive = {c: 0 for c in ids}
    overlap = {a: {b: 0 for b in ids} for a in ids}
    for chosen in by_voter.values():
        for a in chosen:
            yes_count[a] += 1
            if len(chosen) == 1:
                exclusive[a] += 1
            for b in chosen:
                overlap[a][b] += 1
    pledged_total = sum(yes_count.values())
    multi = sum(1 for s in by_voter.values() if len(s) > 1)
    limit = max_votes()
    over = sum(1 for s in by_voter.values() if limit and len(s) > limit)
    away = {r[0] for r in db.execute(f"SELECT national_id FROM pledge_noshow WHERE national_id IN ({sub})", params)}
    expected = {c: 0 for c in ids}  # Yes from voters who are expected to vote
    for nid, chosen in by_voter.items():
        if nid not in away:
            for c in chosen:
                expected[c] += 1

    # by box: yes per candidate per box
    box_rows = db.execute(
        f"SELECT v.location AS box, p.candidate_id AS cid, COUNT(*) AS n FROM pledges p JOIN voters v"
        f" ON v.national_id = p.national_id WHERE p.value = 'yes' AND p.national_id IN ({sub})"
        " GROUP BY v.location, p.candidate_id", params).fetchall()
    box_total = dict(db.execute(f"SELECT location, COUNT(*) FROM voters{where} GROUP BY location", params).fetchall())
    boxes = {}
    for r in box_rows:
        boxes.setdefault(r["box"], {})[r["cid"]] = r["n"]
    leader = {}
    for b in box_total:
        counts = boxes.get(b, {})
        best = max(cands, key=lambda c: counts.get(c["id"], 0), default=None)
        leader[b] = best["name"] if best is not None and counts.get(best["id"], 0) else ""
    # ?bs= box | voters | lead | c<id> (pledges) | p<id> (% of the box) and ?bd= asc | desc
    box_sort = request.args.get("bs", "voters")
    valid = {"box", "voters", "lead"} | {f"c{c['id']}" for c in cands} | {f"p{c['id']}" for c in cands}
    box_sort = box_sort if box_sort in valid else "voters"
    default_dir = "asc" if box_sort in ("box", "lead") else "desc"
    box_dir = request.args.get("bd", default_dir)
    box_dir = box_dir if box_dir in ("asc", "desc") else default_dir

    def box_key(b):
        name = (b or "~").casefold()  # "No box" last
        if box_sort == "box":
            return (b == "", name)
        if box_sort == "voters":
            return (box_total[b], name)
        if box_sort == "lead":
            return (leader[b] == "", leader[b].casefold(), name)
        n = boxes.get(b, {}).get(int(box_sort[1:]), 0)
        return ((n / box_total[b]) if box_sort[0] == "p" and box_total[b] else n, name)

    box_list = sorted(box_total, key=box_key, reverse=box_dir == "desc")

    # by age group and sex
    groups = []
    for label, lo, hi in AGE_GROUPS:
        groups.append((label, "age BETWEEN ? AND ?", [lo, hi]))
    groups += [("Women", "sex = 'F'", []), ("Men", "sex = 'M'", [])]
    group_rows = []
    for label, cond, extra in groups:
        w = f"{where} AND {cond}" if where else f" WHERE {cond}"
        n = db.execute(f"SELECT COUNT(*) FROM voters{w}", params + extra).fetchone()[0]
        counts = dict(db.execute(
            f"SELECT candidate_id, COUNT(*) FROM pledges WHERE value = 'yes' AND national_id IN"
            f" (SELECT national_id FROM voters{w}) GROUP BY candidate_id", params + extra).fetchall())
        group_rows.append((label, n, counts))

    # per week (last 8 weeks): new Yes marks per candidate
    start = datetime.now(timezone.utc) - timedelta(weeks=8)
    weeks = []
    for k in range(8):
        a = start + timedelta(weeks=k)
        weeks.append(((a + timedelta(hours=5)).strftime("%-d %b"), a.isoformat(timespec="seconds"),
                      (a + timedelta(weeks=1)).isoformat(timespec="seconds")))
    trend = []
    for label, a, b in weeks:
        counts = dict(db.execute(
            f"SELECT candidate_id, COUNT(*) FROM pledges WHERE value = 'yes' AND at >= ? AND at < ?"
            f" AND national_id IN ({sub}) GROUP BY candidate_id", [a, b] + params).fetchall())
        trend.append((label, counts))
    trend_max = max([max(c.values()) for _, c in trend if c] or [1])

    w, p = _scoped_where(" WHERE location != ''")
    box_options = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template(
        "pledges/distribution.html", cands=cands, total=total, yes=yes_count, maybe=dict(maybe), exclusive=exclusive,
        overlap=overlap, pledged_total=pledged_total, pledged_voters=len(by_voter), any_mark=any_mark,
        not_pledged=total - len(by_voter), multi=multi, max_votes=limit, over=over, noshows=len(away),
        expected=expected, boxes=boxes, box_total=box_total, leader=leader, box_sort=box_sort, box_dir=box_dir,
        box_list=box_list, group_rows=group_rows, trend=trend, trend_max=trend_max, f=f, box_options=box_options,
        query_args={k: v for k, v in request.args.items()})
