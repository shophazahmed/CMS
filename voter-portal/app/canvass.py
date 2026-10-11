"""Canvassing progress: the dashboard, an agent's own houses, and assigning houses to agents."""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import audit
from .auth import role_required
from .db import get_db, now_iso

bp = Blueprint("canvass", __name__)

LOCAL_OFFSET = timedelta(hours=5)  # Maldives time, no daylight saving
REACHED = ("contacted", "followup", "noanswer")
SORTS = {
    "progress": "reached * 1.0 / total, total DESC, house",
    "remaining": "(total - reached) DESC, house",
    "name": "house COLLATE NOCASE",
    "voters": "total DESC, house",
    "recent": "last_at IS NULL, last_at DESC",
}

HOUSE_STATS = """
SELECT v.house_name AS house,
       COUNT(*) AS total,
       SUM(v.status = 'contacted') AS contacted,
       SUM(v.status = 'followup') AS followup,
       SUM(v.status = 'noanswer') AS noanswer,
       SUM(v.status != 'new') AS reached,
       MAX(v.status_at) AS last_at,
       a.user_id AS agent_id,
       u.username AS agent,
       u.is_active AS agent_active
FROM voters v
LEFT JOIN house_assignments a ON a.house_name = v.house_name
LEFT JOIN users u ON u.id = a.user_id
"""


def local_day_start_utc():
    local_now = datetime.now(timezone.utc) + LOCAL_OFFSET
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start_local - LOCAL_OFFSET).isoformat(timespec="seconds")


def house_rows(where="", params=(), sort="progress"):
    sql = f"{HOUSE_STATS} WHERE v.house_name != '' {where} GROUP BY v.house_name ORDER BY {SORTS[sort]}"
    return get_db().execute(sql, list(params)).fetchall()


def totals(rows):
    t = {"houses": len(rows), "total": 0, "contacted": 0, "followup": 0, "noanswer": 0, "reached": 0,
         "done_houses": 0}
    for r in rows:
        for k in ("total", "contacted", "followup", "noanswer", "reached"):
            t[k] += r[k] or 0
        t["done_houses"] += 1 if r["total"] and r["reached"] == r["total"] else 0
    t["new"] = t["total"] - t["reached"]
    t["pct"] = round(100 * t["reached"] / t["total"]) if t["total"] else 0
    return t


def activity(user_id=None):
    db = get_db()
    since_today = local_day_start_utc()
    since_week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    cond, params = ("AND user_id = ?", [user_id]) if user_id else ("", [])
    count = lambda since: db.execute(  # noqa: E731
        f"SELECT COUNT(*) FROM audit_log WHERE action = 'voter_canvass' AND at >= ? {cond}", [since] + params
    ).fetchone()[0]
    leaders = db.execute(
        "SELECT username, COUNT(*) AS n, MAX(at) AS last FROM audit_log"
        " WHERE action = 'voter_canvass' AND at >= ? GROUP BY username ORDER BY n DESC LIMIT 10",
        (since_week,),
    ).fetchall()
    return {"today": count(since_today), "week": count(since_week), "leaders": leaders}


@bp.route("/dashboard")
@role_required("admin", "user")
def dashboard():
    db = get_db()
    party = request.args.get("party", "")
    agent = request.args.get("agent", "")
    sort = request.args.get("sort", "progress")
    if sort not in SORTS:
        sort = "progress"
    where, params = "", []
    if party:
        where += " AND v.party = ?"
        params.append(party)
    if agent == "none":
        where += " AND a.user_id IS NULL"
    elif agent.isdigit():
        where += " AND a.user_id = ?"
        params.append(int(agent))
    rows = house_rows(where, params, sort)
    parties = [r[0] for r in db.execute("SELECT DISTINCT party FROM voters WHERE party != '' ORDER BY party")]
    agents = db.execute("SELECT id, username FROM users WHERE role = 'agent' ORDER BY username").fetchall()
    no_house = db.execute("SELECT COUNT(*) FROM voters WHERE house_name = ''").fetchone()[0]
    return render_template(
        "canvass/dashboard.html", rows=rows, t=totals(rows), act=activity(), parties=parties, agents=agents,
        party=party, agent=agent, sort=sort, sorts=SORTS, no_house=no_house,
    )


@bp.route("/my")
@role_required("agent", "admin")
def my_houses():
    """An agent's own houses. Admins can look at any agent's page with ?agent=<id>."""
    user = g.user
    if g.user["role"] == "admin":
        agent_id = request.args.get("agent", type=int)
        if agent_id:
            user = get_db().execute("SELECT * FROM users WHERE id = ?", (agent_id,)).fetchone() or abort(404)
    sort = request.args.get("sort", "progress")
    if sort not in SORTS:
        sort = "progress"
    rows = house_rows(" AND a.user_id = ?", [user["id"]], sort)
    t = totals(rows)
    # Suggested next stop: follow-ups first (someone asked us to come back), then the house
    # with the most voters nobody has reached yet.
    open_rows = [r for r in rows if r["reached"] < r["total"] or r["followup"]]
    suggestion = None
    if open_rows:
        suggestion = max(open_rows, key=lambda r: (r["followup"] or 0, r["total"] - r["reached"], -len(r["house"])))
    return render_template("canvass/my.html", rows=rows, t=t, act=activity(user["id"]), agent=user,
                           suggestion=suggestion, sort=sort, viewing_other=user["id"] != g.user["id"])


# --- admin: agents and house assignments ---------------------------------------------

def _agents(active_only=False):
    cond = " AND u.is_active = 1" if active_only else ""
    return get_db().execute(
        f"""SELECT u.id, u.username, u.is_active, u.last_login_at,
                   COUNT(DISTINCT a.house_name) AS houses,
                   COUNT(v.national_id) AS voters,
                   COUNT(v.national_id) AS total,
                   SUM(v.status != 'new') AS reached,
                   SUM(v.status = 'contacted') AS contacted,
                   SUM(v.status = 'followup') AS followup,
                   SUM(v.status = 'noanswer') AS noanswer
            FROM users u
            LEFT JOIN house_assignments a ON a.user_id = u.id
            LEFT JOIN voters v ON v.house_name = a.house_name
            WHERE u.role = 'agent'{cond}
            GROUP BY u.id ORDER BY u.username COLLATE NOCASE"""
    ).fetchall()


@bp.route("/admin/agents")
@role_required("admin")
def agents():
    db = get_db()
    houses = house_rows(sort="name")
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    weekly = dict(db.execute(
        "SELECT user_id, COUNT(*) FROM audit_log WHERE action = 'voter_canvass' AND at >= ? GROUP BY user_id",
        (week,),
    ).fetchall())
    return render_template("canvass/agents.html", agents=_agents(), houses=houses, weekly=weekly,
                           unassigned=sum(1 for h in houses if h["agent_id"] is None))


@bp.post("/admin/agents/assign")
@role_required("admin")
def assign():
    """Save the agent chosen for each house in the table."""
    db = get_db()
    valid = {r["id"] for r in _agents()}
    houses = request.form.getlist("house")
    chosen = request.form.getlist("agent")
    if len(houses) != len(chosen):
        abort(400)
    current = dict(db.execute("SELECT house_name, user_id FROM house_assignments").fetchall())
    changed = 0
    for house, agent in zip(houses, chosen):
        agent_id = int(agent) if agent.isdigit() and int(agent) in valid else None
        if current.get(house) == agent_id:
            continue
        if agent_id is None:
            db.execute("DELETE FROM house_assignments WHERE house_name = ?", (house,))
        else:
            db.execute(
                "INSERT INTO house_assignments (house_name, user_id, assigned_at, assigned_by) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(house_name) DO UPDATE SET user_id = excluded.user_id,"
                " assigned_at = excluded.assigned_at, assigned_by = excluded.assigned_by",
                (house, agent_id, now_iso(), g.user["id"]),
            )
        changed += 1
    if changed:
        audit.log("houses_assigned", detail={"changed": changed}, commit=False)
    db.commit()
    flash(f"Saved {changed} house assignment{'s' if changed != 1 else ''}." if changed else "Nothing changed.",
          "success" if changed else "info")
    return redirect(url_for("canvass.agents"))


def plan_assignment(houses, agent_ids, current_load):
    """Spread houses over agents so each gets a similar amount of work left.

    Work for a house = voters not yet reached + follow-ups (at least 1). Biggest houses go first,
    each to the agent with the least work so far (longest-processing-time first).
    """
    load = {a: current_load.get(a, 0) for a in agent_ids}
    plan = {}
    for h in sorted(houses, key=lambda h: (-h["work"], h["house"])):
        agent = min(load, key=lambda a: (load[a], a))
        plan[h["house"]] = agent
        load[agent] += h["work"]
    return plan, load


@bp.post("/admin/agents/auto")
@role_required("admin")
def auto_assign():
    db = get_db()
    active = {r["id"]: r["username"] for r in _agents(active_only=True)}
    agent_ids = [int(a) for a in request.form.getlist("agents") if a.isdigit() and int(a) in active]
    if not agent_ids:
        flash("Pick at least one active agent.", "error")
        return redirect(url_for("canvass.agents"))
    rebalance = request.form.get("scope") == "all"
    rows = house_rows(sort="name")
    work = lambda r: max(1, (r["total"] - r["reached"]) + (r["followup"] or 0))  # noqa: E731
    current_load = {}
    houses = []
    for r in rows:
        if rebalance or r["agent_id"] is None:
            houses.append({"house": r["house"], "work": work(r)})
        elif r["agent_id"] in agent_ids:
            current_load[r["agent_id"]] = current_load.get(r["agent_id"], 0) + work(r)
    if not houses:
        flash("Every house already has an agent. Choose 'Re-balance all houses' to redistribute.", "info")
        return redirect(url_for("canvass.agents"))
    plan, load = plan_assignment(houses, agent_ids, current_load)
    if rebalance:
        db.execute("DELETE FROM house_assignments")
    stamp = now_iso()
    db.executemany(
        "INSERT INTO house_assignments (house_name, user_id, assigned_at, assigned_by) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(house_name) DO UPDATE SET user_id = excluded.user_id, assigned_at = excluded.assigned_at,"
        " assigned_by = excluded.assigned_by",
        [(house, agent, stamp, g.user["id"]) for house, agent in plan.items()],
    )
    summary = ", ".join(f"{active[a]} {load[a]}" for a in agent_ids)
    audit.log("houses_auto_assigned", detail={"houses": len(plan), "rebalance": rebalance, "load": summary},
              commit=False)
    db.commit()
    flash(f"Assigned {len(plan)} houses. Work per agent (voters left to reach + follow-ups): {summary}.", "success")
    return redirect(url_for("canvass.agents"))
