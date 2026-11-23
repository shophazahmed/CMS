"""Agent assignment sheet: every voter, like the pledge sheet, with the responsible agent on each row.

Pick an agent in a row's drop-down and it saves straight away (the voter is handed to that agent one by
one; "House agent" gives them back to whoever has their house). Tick rows, or take everyone matching the
filters, to hand many at once. The War Room's tier 2 runners work exactly these voters.
"""
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from . import pledges
from .auth import admin_required
from .db import get_db

bp = Blueprint("agentsheet", __name__)


def _agents():
    return get_db().execute(
        "SELECT id, username, role, can_login, war_tier FROM users WHERE role IN ('agent', 'user', 'admin')"
        " AND deleted_at IS NULL ORDER BY username COLLATE NOCASE").fetchall()


def _where():
    """The pledge sheet filters (search, box, house, sex, age, pledges, pledge pattern...) plus ?ag=."""
    from .voters import handled_by

    f, where, params, cands, pc, pv = pledges._filters()
    ag = request.args.get("ag", "")
    if ag == "none" or ag.isdigit():
        cond = ("(national_id NOT IN (SELECT national_id FROM voter_assignments) AND house_name NOT IN"
                " (SELECT house_name FROM house_assignments))") if ag == "none" else handled_by()
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
        params = list(params) + ([] if ag == "none" else [int(ag), int(ag)])
    f["ag"] = ag if ag == "none" or ag.isdigit() else ""
    return f, where, params, cands, pc, pv


@bp.route("/agents/sheet")
@admin_required
def sheet():
    f, where, params, cands, pc, pv = _where()
    sort_key, sort_dir, order = pledges.sheet_order()
    db = get_db()
    size = request.args.get("n", type=int)
    size = size if size in pledges.PAGE_SIZES else 50
    total = db.execute(f"SELECT COUNT(*) FROM voters{where}", params).fetchone()[0]
    pages = max(1, -(-total // size))
    try:
        page = min(max(1, int(request.args.get("page", 1))), pages)
    except ValueError:
        page = 1
    rows = db.execute(
        "SELECT * FROM (SELECT v.*, va.user_id AS one_by_one, ha.user_id AS house_agent FROM voters v"
        " LEFT JOIN voter_assignments va ON va.national_id = v.national_id"
        " LEFT JOIN house_assignments ha ON ha.house_name = v.house_name"
        f" WHERE v.national_id IN (SELECT national_id FROM voters{where})) ORDER BY {order} LIMIT ? OFFSET ?",
        params + [size, (page - 1) * size]).fetchall()
    # how many voters each agent has in this list (one by one first, else the house's agent)
    counts = dict(db.execute(
        "SELECT COALESCE(va.user_id, ha.user_id) AS who, COUNT(*) FROM voters v"
        " LEFT JOIN voter_assignments va ON va.national_id = v.national_id"
        " LEFT JOIN house_assignments ha ON ha.house_name = v.house_name"
        f" WHERE v.national_id IN (SELECT national_id FROM voters{where}) GROUP BY who", params).fetchall())
    agents = _agents()
    w, p = pledges._scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template("agents/sheet.html", rows=rows, agents=agents, names={a["id"]: a["username"] for a in agents},
                           counts=counts, total=total, page=page, pages=pages, size=size, sizes=pledges.PAGE_SIZES,
                           f=f, pc=pc, pv=pv, cands=cands, boxes=boxes, sort_key=sort_key, sort_dir=sort_dir,
                           query_args={k: v for k, v in request.args.items() if k != "page"},
                           first=(page - 1) * size, max_bulk=5000)


@bp.post("/agents/sheet/set")
@admin_required
def set_one():
    """One row's drop-down: hand the voter to an agent, or back to their house's agent ('')."""
    from .voters import _chosen_agent, assign_voters, get_voter_or_404

    nid = request.form.get("national_id", "")
    get_voter_or_404(nid)
    agent_id, name = _chosen_agent(request.form.get("agent", ""))
    assign_voters([nid], agent_id)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "agent": name or ""})
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/")
                    else url_for("agentsheet.sheet"))


@bp.post("/agents/sheet/assign")
@admin_required
def assign_many():
    """The ticked rows, or every voter matching the sheet's filters (the query string), to one agent."""
    from .voters import _chosen_agent, assign_voters

    agent_id, name = _chosen_agent(request.form.get("agent", ""))
    db = get_db()
    if request.form.get("scope") == "all":
        f, where, params, *_ = _where()
        ids = [r[0] for r in db.execute(f"SELECT national_id FROM voters{where} LIMIT 5001", params)]
        if len(ids) > 5000:
            flash("That's more than 5000 voters. Narrow the filters first.", "error")
            return redirect(url_for("agentsheet.sheet", **request.args))
    else:
        wanted = list(dict.fromkeys(request.form.getlist("ids")))[:5000]
        ids = [r[0] for r in db.execute(f"SELECT national_id FROM voters WHERE national_id IN ({','.join('?' * len(wanted))})",
                                        wanted)] if wanted else []
    if not ids:
        flash("Tick some voters first, or choose everyone in the list.", "error")
    else:
        n = assign_voters(ids, agent_id)
        flash(f"{n} voter{'s' if n != 1 else ''} now with {name or 'their house agent'}." if n else "Nothing changed.",
              "success" if n else "info")
    return redirect(url_for("agentsheet.sheet", **request.args))
