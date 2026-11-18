"""House visits: who went to which house, what happened, who was met, and when to go back.

A voter's box (voters.location, from the official lists) is where they live and vote; the Visits
list, the house page and the dashboards can be filtered by it.

Like canvassing status and notes, visits are personal: users and agents see their own visits,
admins see everyone's.
"""
import csv
import io
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from . import audit, islandmap, personal
from .auth import admin_required, login_required
from .db import get_db, now_iso
from .normalize import clean_text

bp = Blueprint("visits", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS house_visits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    house_name      TEXT NOT NULL,
    visited_at      TEXT NOT NULL,
    user_id         INTEGER,
    username        TEXT,
    outcome         TEXT NOT NULL CHECK (outcome IN ('met', 'nobody', 'later', 'refused')),
    needs_transport INTEGER NOT NULL DEFAULT 0,
    needs_senior    INTEGER NOT NULL DEFAULT 0,
    notes           TEXT NOT NULL DEFAULT '',
    next_visit_on   TEXT              -- YYYY-MM-DD, Maldives date
);
CREATE INDEX IF NOT EXISTS ix_visits_house ON house_visits (house_name, visited_at);
CREATE INDEX IF NOT EXISTS ix_visits_user ON house_visits (user_id, visited_at);
CREATE TABLE IF NOT EXISTS visit_voters (
    visit_id    INTEGER NOT NULL REFERENCES house_visits(id) ON DELETE CASCADE,
    national_id TEXT NOT NULL,
    PRIMARY KEY (visit_id, national_id)
);
"""

OUTCOMES = {"met": "Met family", "nobody": "Nobody home", "later": "Come back later", "refused": "Refused"}
MAX_NOTES = 1000
LOCAL_OFFSET = timedelta(hours=5)


FILTERS = {
    "": "All houses",
    "todo": "To visit",
    "due": "Due today",
    "overdue": "Overdue",
    "never": "Never visited",
    "nobody": "Nobody home last time",
    "week": "Visited this week",
    "transport": "Needs transport",
    "senior": "Needs a senior visit",
}


@bp.app_template_filter("day")
def day_label(value):
    """'2026-11-21' -> '21 Nov'."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%-d %b")
    except (TypeError, ValueError):
        return value or ""


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def today():
    return (datetime.now(timezone.utc) + LOCAL_OFFSET).date().isoformat()




def _visits_scope(alias="x"):
    """Users and agents see only their own visits; admins everyone's."""
    if personal.sees_everyone():
        return "", []
    return f" AND {alias}.user_id = ?", [g.user["id"]]


def house_list():
    """Every house with voters in your scope, with your (or, for admins, everyone's) visits."""
    from .voters import scoped

    src = personal.source()
    w, p = scoped(" WHERE deceased = 0")
    vs, vp = _visits_scope("x")
    latest = (f"(SELECT {{col}} FROM house_visits x WHERE x.house_name = h.house{vs}"
              " ORDER BY x.visited_at DESC, x.id DESC LIMIT 1)")
    sql = f"""
        WITH r AS (SELECT house_name AS house, status, location AS box FROM {src}{w}),
             h AS (SELECT house, COUNT(*) AS total, SUM(status != 'new') AS reached,
                          SUM(box != '') AS with_box FROM r
                   WHERE house IS NOT NULL AND house != '' GROUP BY house)
        SELECT h.house, h.total, h.reached,
               (SELECT group_concat(b, char(31)) FROM (SELECT DISTINCT r3.box AS b FROM r r3
                 WHERE r3.house = h.house AND r3.box != '' ORDER BY r3.box)) AS boxes,
               (SELECT r2.box FROM r r2 WHERE r2.house = h.house AND r2.box != '' GROUP BY r2.box
                ORDER BY COUNT(*) DESC, r2.box LIMIT 1) AS box,
               (SELECT COUNT(*) FROM house_visits x WHERE x.house_name = h.house{vs}) AS visits,
               {latest.format(col='visited_at')} AS last_at,
               {latest.format(col='outcome')} AS last_outcome,
               {latest.format(col='username')} AS last_by,
               {latest.format(col='next_visit_on')} AS next_on,
               {latest.format(col='needs_transport')} AS transport,
               {latest.format(col='needs_senior')} AS senior,
               a.user_id AS agent_id, u.username AS agent
        FROM h LEFT JOIN house_assignments a ON a.house_name = h.house LEFT JOIN users u ON u.id = a.user_id
    """
    if "visit_houses" not in g:  # used more than once per page
        g.visit_houses = get_db().execute(sql, p + vp * 7).fetchall()
    return g.visit_houses


def _matches(row, key, now, week_ago):
    due = row["next_on"]
    if key == "todo":
        # never visited, due, or nobody met last time and no later date set
        return (not row["visits"] or (due is not None and due <= now)
                or (due is None and row["last_outcome"] in ("nobody", "later")))
    if key == "due":
        return due == now
    if key == "overdue":
        return due is not None and due < now
    if key == "never":
        return not row["visits"]
    if key == "nobody":
        return row["last_outcome"] == "nobody"
    if key == "week":
        return bool(row["last_at"]) and row["last_at"] >= week_ago
    if key == "transport":
        return bool(row["transport"])
    if key == "senior":
        return bool(row["senior"])
    return True


def _plan_order(row, now):
    """Overdue and due first (earliest first), then never visited, then the longest since a visit."""
    due = row["next_on"]
    if due is not None and due <= now:
        return (0, due, row["house"].casefold())
    if not row["visits"]:
        return (1, "", row["house"].casefold())
    if due is None and row["last_outcome"] in ("nobody", "later"):
        return (2, row["last_at"] or "", row["house"].casefold())
    return (3, row["last_at"] or "", row["house"].casefold())


SORTS = {"plan": "To visit first", "box": "Box (where they vote)", "voters": "Most voters",
         "fewest": "Fewest voters", "last": "Recently visited", "name": "House name"}
NO_BOX = "(none)"


BOX_SEP = "\x1f"  # box names can contain commas, so they're joined with a control character


def house_boxes(row):
    return [b for b in (row["boxes"] or "").split(BOX_SEP) if b]


def in_box(rows, box):
    if box == NO_BOX:
        return [r for r in rows if not house_boxes(r)]
    if box:
        return [r for r in rows if box in house_boxes(r)]
    return rows


def filtered_houses(key="", q="", agent="", sort="plan", box=""):
    now = today()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    rows = house_list()
    if q:
        rows = [r for r in rows if q.casefold() in r["house"].casefold()]
    if agent == "none":
        rows = [r for r in rows if r["agent_id"] is None]
    elif agent.isdigit():
        rows = [r for r in rows if r["agent_id"] == int(agent)]
    rows = in_box(rows, box)
    # chip counts follow the search, agent and box, so they match what a tap shows
    counts = {k: sum(1 for r in rows if _matches(r, k, now, week_ago)) for k in FILTERS}
    out = [r for r in rows if _matches(r, key, now, week_ago)]
    if sort == "box":
        out.sort(key=lambda r: (r["box"] is None, (r["box"] or "").casefold(), r["house"].casefold()))
    elif sort == "fewest":
        out.sort(key=lambda r: (r["total"], r["house"].casefold()))
    elif sort == "last":
        out.sort(key=lambda r: r["last_at"] or "", reverse=True)
    elif sort == "name":
        out.sort(key=lambda r: r["house"].casefold())
    elif sort == "voters":
        out.sort(key=lambda r: (-r["total"], r["house"].casefold()))
    else:
        out.sort(key=lambda r: _plan_order(r, now))
    return out, counts, now


@bp.route("/visits")
@login_required
def index():
    key = request.args.get("f", "")
    key = key if key in FILTERS else ""
    q = clean_text(request.args.get("q", ""))[:100]
    agent = request.args.get("agent", "") if personal.sees_everyone() or g.user["role"] == "user" else ""
    sort = request.args.get("sort", "plan")
    sort = sort if sort in SORTS else "plan"
    box = clean_text(request.args.get("box", ""))[:200]
    rows, counts, now = filtered_houses(key, q, agent, sort, box)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    size = 50
    pages = max(1, -(-len(rows) // size))
    page = min(page, pages)
    agents = get_db().execute(
        "SELECT id, username FROM users WHERE role = 'agent' AND deleted_at IS NULL ORDER BY username COLLATE NOCASE"
    ).fetchall() if g.user["role"] != "agent" else []
    query_args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("visits/index.html", rows=rows[(page - 1) * size:page * size], total=len(rows),
                           counts=counts, filters=FILTERS, key=key, q=q, agent=agent, agents=agents, sort=sort,
                           sorts=SORTS, outcomes=OUTCOMES, today=now, page=page, pages=pages,
                           query_args=query_args, box=box, boxes=box_options(),
                           no_box=NO_BOX, house_boxes=house_boxes)


def box_options():
    """Boxes (voters.location) of the people you can see, with how many houses vote there."""
    counts = {}
    for r in house_list():
        for b in house_boxes(r):
            counts[b] = counts.get(b, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[0].casefold())




def _house_people(house, box=""):
    """The voters registered at a house, within your scope (and one box, when given)."""
    from .voters import scoped

    sql, params = " WHERE deceased = 0 AND house_name = ?", [house]
    if box:
        sql, params = sql + " AND location = ?", params + ["" if box == NO_BOX else box]
    w, p = scoped(sql, params)
    order = " ORDER BY age IS NULL, age DESC, full_name COLLATE NOCASE"
    return get_db().execute(f"SELECT * FROM {personal.source()}{w}{order}", p).fetchall()


def _visible_house_or_404(house, box=""):
    if not house:
        abort(404)
    everyone = _house_people(house)
    if not everyone:
        abort(404)
    return everyone, ([v for v in everyone if v["location"] == ("" if box == NO_BOX else box)] if box else everyone)


@bp.route("/visits/house")
@login_required
def house():
    name = request.args.get("h", "")
    box = request.args.get("box", "")[:200]
    everyone, residents = _visible_house_or_404(name, box)
    boxes = sorted({v["location"] for v in everyone if v["location"]}, key=str.casefold)
    db = get_db()
    vs, vp = _visits_scope("x")
    history = db.execute(
        f"SELECT x.* FROM house_visits x WHERE x.house_name = ?{vs} ORDER BY x.visited_at DESC, x.id DESC LIMIT 100",
        [name] + vp).fetchall()
    met = {}
    if history:
        ids = [h["id"] for h in history]
        for row in db.execute(
                "SELECT vv.visit_id, COALESCE(v.full_name, vv.national_id) AS name FROM visit_voters vv"
                f" LEFT JOIN voters v ON v.national_id = vv.national_id WHERE vv.visit_id IN ({','.join('?' * len(ids))})"
                " ORDER BY name", ids):
            met.setdefault(row["visit_id"], []).append(row["name"])
    agent = db.execute("SELECT u.username FROM house_assignments a JOIN users u ON u.id = a.user_id"
                       " WHERE a.house_name = ?", (name,)).fetchone()
    from .voters import STATUSES

    return render_template("visits/house.html", house=name, residents=residents, everyone=len(everyone),
                           box=box, boxes=boxes, no_box=NO_BOX, has_no_box=any(not v["location"] for v in everyone),
                           history=history, met=met, outcomes=OUTCOMES, statuses=STATUSES, agent=agent, today=today(),
                           map_ready=islandmap.has_map(), map_loc=islandmap.location(name),
                           back=request.full_path.rstrip("?"))


def _set_own_status(national_id, status, stamp, visit_id):
    """Same as saving a status on a voter card: the person's own status, and the latest for admins."""
    db = get_db()
    uid = g.user["id"]
    mine = db.execute("SELECT status FROM canvass WHERE national_id = ? AND user_id = ?", (national_id, uid)).fetchone()
    current = mine["status"] if mine else "new"
    if current == status:
        return False
    db.execute("INSERT INTO canvass (national_id, user_id, status, status_at) VALUES (?, ?, ?, ?)"
               " ON CONFLICT(national_id, user_id) DO UPDATE SET status = excluded.status,"
               " status_at = excluded.status_at", (national_id, uid, status, stamp))
    db.execute("UPDATE voters SET status = ?, status_at = ?, status_by = ? WHERE national_id = ?",
               (status, stamp, uid, national_id))
    audit.log("voter_canvass", target=national_id,
              detail={"status": {"from": current, "to": status}, "visit": visit_id}, commit=False)
    return True


def statuses_for(outcome, residents, met_ids):
    """What a visit does to the visitor's own status, never undoing a better one:
    met / refused: everyone ticked is Contacted;
    come back later: ticked (or, with nobody ticked, everyone not yet reached) is Follow up;
    nobody home: everyone not contacted yet is No answer."""
    changes = {}
    for v in residents:
        nid, current = v["national_id"], v["status"]
        if outcome in ("met", "refused") and nid in met_ids:
            changes[nid] = "contacted"
        elif outcome == "later" and (nid in met_ids or (not met_ids and current in ("new", "noanswer"))):
            changes[nid] = "followup"
        elif outcome == "nobody" and current == "new":
            changes[nid] = "noanswer"
    return changes


def _parse_next(text):
    if not text:
        return None
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return day.isoformat()


@bp.post("/visits/house")
@login_required
def record():
    name = request.form.get("h", "")
    box = request.form.get("box", "")[:200]
    _, residents = _visible_house_or_404(name, box)  # with a box chosen, only the voters shown
    back = url_for("visits.house", h=name, box=box or None)
    outcome = request.form.get("outcome", "")
    if outcome not in OUTCOMES:
        abort(400)
    resident_ids = {v["national_id"] for v in residents}
    met_ids = {nid for nid in request.form.getlist("met") if nid in resident_ids}
    if outcome in ("met", "refused") and not met_ids and residents:
        flash("Tick who you met, or choose “Nobody home”.", "error")
        return redirect(back)
    next_on = _parse_next(request.form.get("next_visit_on", ""))
    if next_on and next_on < today():
        flash("The next visit can't be in the past.", "error")
        return redirect(back)
    notes = request.form.get("notes", "").replace("\r\n", "\n").strip()[:MAX_NOTES]
    db = get_db()
    stamp = now_iso()
    cur = db.execute(
        "INSERT INTO house_visits (house_name, visited_at, user_id, username, outcome, needs_transport, needs_senior,"
        " notes, next_visit_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, stamp, g.user["id"], g.user["username"], outcome, 1 if request.form.get("transport") else 0,
         1 if request.form.get("senior") else 0, notes, next_on))
    visit_id = cur.lastrowid
    db.executemany("INSERT INTO visit_voters (visit_id, national_id) VALUES (?, ?)",
                   [(visit_id, nid) for nid in sorted(met_ids)])
    changed = sum(_set_own_status(nid, status, stamp, visit_id)
                  for nid, status in statuses_for(outcome, residents, met_ids).items())
    audit.log("house_visit", target=name, detail={"visit": visit_id, "outcome": outcome, "met": len(met_ids),
                                                   "next": next_on, "statuses": changed}, commit=False)
    db.commit()
    msg = f"Visit saved: {OUTCOMES[outcome]}."
    if changed:
        msg += f" {changed} voter status{'es' if changed != 1 else ''} updated."
    if next_on:
        msg += f" Next visit {datetime.strptime(next_on, '%Y-%m-%d').strftime('%-d %b')}."
    flash(msg, "success")
    return redirect(back)


@bp.post("/visits/<int:visit_id>/delete")
@admin_required
def delete(visit_id):
    db = get_db()
    visit = db.execute("SELECT * FROM house_visits WHERE id = ?", (visit_id,)).fetchone() or abort(404)
    db.execute("DELETE FROM visit_voters WHERE visit_id = ?", (visit_id,))
    db.execute("DELETE FROM house_visits WHERE id = ?", (visit_id,))
    audit.log("house_visit_delete", target=visit["house_name"],
              detail={"visit": visit_id, "by": visit["username"], "at": visit["visited_at"],
                      "outcome": visit["outcome"]}, commit=False)
    db.commit()
    flash("Visit deleted. Voter statuses it changed stay as they are.", "success")
    return redirect(url_for("visits.house", h=visit["house_name"]))


def dashboard_numbers(box=""):
    """For the admin dashboard: houses visited, visits today / this week, flags (for one box, when given)."""
    rows = in_box(house_list(), box)
    now = today()
    db = get_db()
    day_start = (datetime.strptime(now, "%Y-%m-%d").replace(tzinfo=timezone.utc) - LOCAL_OFFSET).isoformat(
        timespec="seconds")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    names = {r["house"] for r in rows}

    def count(since):
        found = db.execute("SELECT house_name FROM house_visits WHERE visited_at >= ?", (since,)).fetchall()
        return sum(1 for (h,) in found if not box or h in names)
    visited = sum(1 for r in rows if r["visits"])
    return {
        "houses": len(rows), "visited": visited,
        "pct": round(100 * visited / len(rows)) if rows else 0,
        "today": count(day_start), "week": count(week_ago),
        "due": sum(1 for r in rows if r["next_on"] is not None and r["next_on"] <= now),
        "transport": sum(1 for r in rows if r["transport"]),
        "senior": sum(1 for r in rows if r["senior"])
    }


def next_houses(limit=5, box=""):
    """Your next houses to visit (My dashboard): overdue and due, then never visited."""
    rows, _, now = filtered_houses("todo", box=box)
    return rows[:limit], now


@bp.route("/visits/export.csv")
@admin_required
def export():
    from .voters import _safe_cell

    rows = get_db().execute(
        "SELECT x.*, (SELECT group_concat(COALESCE(v.full_name, vv.national_id), '; ') FROM visit_voters vv"
        " LEFT JOIN voters v ON v.national_id = vv.national_id WHERE vv.visit_id = x.id) AS met"
        " FROM house_visits x ORDER BY x.visited_at DESC, x.id DESC").fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Visited", "House", "By", "Outcome", "Met", "Needs transport", "Needs senior visit",
                     "Next visit", "Notes"])
    local = lambda s: (datetime.fromisoformat(s) + LOCAL_OFFSET).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    for r in rows:
        writer.writerow([local(r["visited_at"]), _safe_cell(r["house_name"]), _safe_cell(r["username"] or ""),
                         OUTCOMES[r["outcome"]], _safe_cell(r["met"] or ""), "yes" if r["needs_transport"] else "",
                         "yes" if r["needs_senior"] else "", r["next_visit_on"] or "", _safe_cell(r["notes"])])
    audit.log("visits_export", detail={"rows": len(rows)})
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="house-visits-{stamp}.csv"'})
