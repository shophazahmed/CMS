"""Reports: ready-made tables (default reports) and your own cross-tables (multi reports).

Every report is a table of voter counts: one thing down the side (rows), optionally another across
the top (columns), with the same filters as the pledge sheet (box, house, sex, age, agent, pledges...).
The default reports are such tables set up in advance. Each can be viewed, shown as counts or row %,
and downloaded (Excel / CSV); several can be downloaded together, one tab per report. Pledge and War
Room figures appear only for people who may already see them. Admins only.
"""
import csv
import io
from datetime import datetime

from flask import Blueprint, Response, abort, render_template, request

from . import audit, pledges
from .auth import admin_required, can_view_pledges
from .db import get_db
from .voters import STATUSES

bp = Blueprint("reports", __name__)

NONE = "(none)"
AGE_SQL = ("CASE WHEN v.age IS NULL THEN 'Age unknown' WHEN v.age <= 30 THEN '18–30' WHEN v.age <= 45 THEN '31–45'"
           " WHEN v.age <= 60 THEN '46–60' ELSE '61+' END")
AGE_ORDER = ["18–30", "31–45", "46–60", "61+", "Age unknown"]


def _war_ok():
    from .warroom import can_watch

    return can_watch()


def dimensions():
    """What a report can be split by: key -> (label, SQL expression on v, joins it needs, value labels, order)."""
    from .warroom import CONTACT

    dims = {
        "box": ("Box", f"COALESCE(NULLIF(v.location, ''), '{NONE}')", (), None, None),
        "house": ("House", f"COALESCE(NULLIF(v.house_name, ''), '{NONE}')", (), None, None),
        "sex": ("Sex", "CASE v.sex WHEN 'M' THEN 'Men' WHEN 'F' THEN 'Women' ELSE 'Not recorded' END", (), None,
                ["Men", "Women", "Not recorded"]),
        "age": ("Age group", AGE_SQL, (), None, AGE_ORDER),
        "party": ("Party", f"COALESCE(NULLIF(v.party, ''), '{NONE}')", (), None, None),
        "agent": ("Agent", "COALESCE(ag.username, 'No agent')", ("agent",), None, None),
        "status": ("Canvassing status", "v.status", (), STATUSES, list(STATUSES)),
        "visited": ("House visited", "CASE WHEN v.house_name IN (SELECT house_name FROM house_visits) THEN 'Visited'"
                    " ELSE 'Not visited' END", (), None, ["Visited", "Not visited"]),
    }
    if can_view_pledges():
        cands = pledges.candidates()
        dims["yes"] = ("Yes pledged to", "COALESCE(cy.name, 'No Yes')", ("yes",), None,
                       [c["name"] for c in cands] + ["No Yes"])
        for c in cands:
            dims[f"p{c['id']}"] = (f"Pledge: {c['name']}",
                                   f"COALESCE((SELECT value FROM pledges WHERE national_id = v.national_id AND candidate_id = {int(c['id'])}), '')",
                                   (), {"yes": "Yes", "maybe": "Maybe", "no": "No", "": "Not marked"},
                                   ["yes", "maybe", "no", ""])
        dims["noshow"] = ("No-show", "CASE WHEN v.national_id IN (SELECT national_id FROM pledge_noshow) THEN 'No-show'"
                          " ELSE 'Expected to vote' END", (), None, ["Expected to vote", "No-show"])
    if _war_ok():
        dims["contact"] = ("Said before the vote", "COALESCE(k.status, '')", ("contact",),
                           dict(CONTACT, **{"": "Not reached yet"}), list(CONTACT) + [""])
        dims["voted"] = ("Voted (election day)", "CASE WHEN v.national_id IN (SELECT national_id FROM war_voted)"
                         " THEN 'Voted' ELSE 'Not yet' END", (), None, ["Voted", "Not yet"])
    return dims


JOINS = {
    "agent": (" LEFT JOIN voter_assignments va ON va.national_id = v.national_id"
              " LEFT JOIN house_assignments ha ON ha.house_name = v.house_name"
              " LEFT JOIN users ag ON ag.id = COALESCE(va.user_id, ha.user_id)"),
    "yes": (" LEFT JOIN pledges py ON py.national_id = v.national_id AND py.value = 'yes'"
            " LEFT JOIN candidates cy ON cy.id = py.candidate_id AND cy.active = 1"),
    "contact": " LEFT JOIN war_contact k ON k.national_id = v.national_id",
}


def presets():
    """The default reports: (key, title, about, rows, columns, extra filters)."""
    out = [
        ("box-sex", "Voters by box and sex", "How many voters each box has, men and women.", "box", "sex", {}),
        ("box-age", "Voters by box and age", "Age groups in each box.", "box", "age", {}),
        ("status-box", "Canvassing by box", "Where canvassing stands in each box.", "box", "status", {}),
        ("status-agent", "Canvassing by agent", "How far each agent has got with their voters.", "agent", "status", {}),
        ("visits-box", "House visits by box", "Voters in houses that have been visited, per box.", "box", "visited", {}),
        ("agents", "Voters per agent", "How many voters each agent looks after, by box.", "agent", "box", {}),
        ("houses", "Voters per house", "Every house with its voter count, by sex.", "house", "sex", {}),
    ]
    if can_view_pledges():
        out += [
            ("yes-box", "Yes pledges by box", "Yes pledges for each candidate in each box (a voter with two Yes counts"
             " under both).", "box", "yes", {}),
            ("yes-agent", "Yes pledges by agent", "Yes pledges among each agent's voters.", "agent", "yes", {}),
            ("yes-age", "Yes pledges by age group", "Which candidates each age group backs.", "age", "yes", {}),
            ("noshow-box", "No-shows by box", "Voters who can't vote, per box.", "box", "noshow", {}),
        ]
    if _war_ok():
        out += [
            ("contact-agent", "Contact before the vote, by agent", "What voters told each agent (and who isn't reached).",
             "agent", "contact", {}),
            ("contact-box", "Contact before the vote, by box", "The same, per box.", "box", "contact", {}),
            ("turnout-box", "Election day turnout by box", "Voters marked voted on the day, per box.", "box", "voted", {}),
        ]
    return out


def build(rows_key, cols_key, args):
    """The cross-table: (row labels, column labels, {(r, c): n}, row totals, column totals, total)."""
    dims = dimensions()
    if rows_key not in dims or (cols_key and cols_key not in dims) or rows_key == cols_key:
        abort(400)
    f, where, params, *_ = pledges._filters()  # the pledge sheet filters (and the access scope)
    r_label, r_sql, r_joins, r_names, r_order = dims[rows_key]
    if cols_key:
        c_label, c_sql, c_joins, c_names, c_order = dims[cols_key]
    else:
        c_label, c_sql, c_joins, c_names, c_order = ("", "'Voters'", (), None, ["Voters"])
    joins = "".join(JOINS[j] for j in dict.fromkeys(tuple(r_joins) + tuple(c_joins)))
    count = "COUNT(*)" if "yes" in (rows_key, cols_key) else "COUNT(DISTINCT v.national_id)"
    data = get_db().execute(
        f"SELECT {r_sql} AS r, {c_sql} AS c, {count} FROM voters v{joins}"
        f" WHERE v.deceased = 0 AND v.national_id IN (SELECT national_id FROM voters{where}) GROUP BY r, c",
        params).fetchall()
    cells = {(r, c): n for r, c, n in data}

    def ordered(values, order, names):
        values = list(dict.fromkeys(values))
        if order:
            values = [v for v in order if v in values] + sorted(v for v in values if v not in order)
        else:
            values = sorted(values, key=lambda v: (v == NONE or v == "No agent", str(v).casefold()))
        return [(v, (names or {}).get(v, v)) for v in values]

    rows = ordered([r for r, _ in cells], r_order, r_names)
    cols = ordered([c for _, c in cells], c_order, c_names)
    row_tot = {r: sum(n for (rr, _), n in cells.items() if rr == r) for r, _ in rows}
    col_tot = {c: sum(n for (_, cc), n in cells.items() if cc == c) for c, _ in cols}
    return {"rows": rows, "cols": cols, "cells": cells, "row_tot": row_tot, "col_tot": col_tot,
            "total": sum(cells.values()), "r_label": r_label, "c_label": c_label, "f": f,
            "max": max(cells.values(), default=0), "pledge_rows": "yes" in (rows_key, cols_key)}


def _filter_args():
    return {k: v for k, v in request.args.items() if k not in ("rows", "cols", "pct", "r", "page")}


@bp.route("/reports")
@admin_required
def index():
    dims = dimensions()
    f, *_ = pledges._filters()
    db = get_db()
    w, p = pledges._scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template("reports/index.html", presets=presets(), dims=dims, f=f, boxes=boxes, lists=lists(),
                           cands=pledges.candidates(), pc="", pv="", query_args=_filter_args())


@bp.route("/reports/view")
@admin_required
def view():
    key = request.args.get("r", "")
    preset = next((p for p in presets() if p[0] == key), None)
    rows_key = preset[3] if preset else request.args.get("rows", "box")
    cols_key = preset[4] if preset else request.args.get("cols", "")
    t = build(rows_key, cols_key, request.args)
    db = get_db()
    w, p = pledges._scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template("reports/view.html", t=t, preset=preset, rows_key=rows_key, cols_key=cols_key,
                           dims=dimensions(), pct=request.args.get("pct", ""), key=key, boxes=boxes,
                           f=t["f"], cands=pledges.candidates(), pc="", pv="", query_args=_filter_args())


def _sheet(t, pct=""):
    """Header + body rows for a file."""
    head = [t["r_label"]] + [label for _, label in t["cols"]] + (["Total"] if len(t["cols"]) > 1 else [])
    body = []
    for r, r_label in t["rows"]:
        line = [r_label]
        for c, _ in t["cols"]:
            n = t["cells"].get((r, c), 0)
            line.append(round(100 * n / t["row_tot"][r], 1) if pct and t["row_tot"][r] else n)
        if len(t["cols"]) > 1:
            line.append(100 if pct else t["row_tot"][r])
        body.append(line)
    foot = ["Total"] + [t["col_tot"][c] for c, _ in t["cols"]] + ([t["total"]] if len(t["cols"]) > 1 else [])
    return head, body, foot


@bp.route("/reports/export.<fmt>")
@admin_required
def export(fmt):
    """One report (?r= a default report, or ?rows=&cols=) as CSV / Excel, or several (?many=key&many=key) as one
    Excel workbook, one tab each."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    from .voters import _safe_cell

    if fmt not in ("csv", "xlsx"):
        abort(404)
    pct = request.args.get("pct", "")
    many = request.args.getlist("many")
    known = {p[0]: p for p in presets()}
    if many:
        if fmt != "xlsx":
            abort(400)
        jobs = [(known[k][1], known[k][3], known[k][4]) for k in many if k in known]
        if not jobs:
            abort(400)
    else:
        key = request.args.get("r", "")
        if key in known:
            jobs = [(known[key][1], known[key][3], known[key][4])]
        else:
            rows_key, cols_key = request.args.get("rows", "box"), request.args.get("cols", "")
            dims = dimensions()
            title = dims.get(rows_key, ("Report",))[0] + (f" by {dims[cols_key][0]}" if cols_key in dims else "")
            jobs = [(title, rows_key, cols_key)]
    tables = [(title, build(r, c, request.args)) for title, r, c in jobs]
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    audit.log("report_export", target=fmt, detail={"reports": [title for title, _ in tables], "pct": bool(pct)})
    if fmt == "csv":
        head, body, foot = _sheet(tables[0][1], pct)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(head)
        w.writerows([[_safe_cell(str(x)) if isinstance(x, str) else x for x in line] for line in body])
        w.writerow(foot)
        return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="report-{stamp}.csv"'})
    wb = Workbook()
    wb.remove(wb.active)
    for title, t in tables:
        ws = wb.create_sheet("".join(ch for ch in title if ch not in '[]:*?/\\')[:31] or "Report")
        ws.append([title])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append([f"Voter counts{' (row %)' if pct else ''} · made {datetime.now().strftime('%-d %b %Y %H:%M')}"
                   + (" · a voter with two Yes counts under both" if t["pledge_rows"] else "")])
        head, body, foot = _sheet(t, pct)
        ws.append(head)
        for cell in ws[3]:
            cell.font = Font(bold=True)
        for line in body:
            ws.append([_safe_cell(x) if isinstance(x, str) else x for x in line])
        ws.append(foot)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "B4"
        ws.column_dimensions["A"].width = 30
    out = io.BytesIO()
    wb.save(out)
    name = f"reports-{stamp}.xlsx" if len(tables) > 1 else f"report-{stamp}.xlsx"
    return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def heat(n, top):
    """A shade class (h0-h5) for a cell, so big numbers stand out."""
    if not top or not n:
        return "h0"
    return f"h{min(5, 1 + int(5 * n / (top + 1)))}"


@bp.app_template_global()
def report_heat(n, top):
    return heat(n, top)



# --- list reports: one row per voter -------------------------------------------------------------

def lists():
    """The list reports this person may open: key -> (title, about)."""
    from .auth import can_view_super

    out = {"voters-agents": ("Voters and their agent", "Every voter with the agent who looks after them (one by one, or"
                                                       " through their house).")}
    if can_view_super() and can_view_pledges():
        out["super-agents"] = ("Super pledge voters and their agent", "Every super pledge voter, which candidates"
                                                                      " they are super for, and their agent.")
    return out


def list_rows(kind):
    f, where, params, cands, *_ = pledges._filters()
    db = get_db()
    names = {c["id"]: c["name"] for c in pledges.candidates(active_only=False)}
    extra = ""
    if kind == "super-agents":
        extra = " AND v.national_id IN (SELECT national_id FROM super_votes)"
    rows = db.execute(
        "SELECT v.national_id, v.full_name, v.sex, v.age, v.house_name, v.location, v.phone,"
        " va.user_id AS one_by_one, COALESCE(ua.username, uh.username) AS agent FROM voters v"
        " LEFT JOIN voter_assignments va ON va.national_id = v.national_id"
        " LEFT JOIN house_assignments ha ON ha.house_name = v.house_name"
        " LEFT JOIN users ua ON ua.id = va.user_id LEFT JOIN users uh ON uh.id = ha.user_id"
        f" WHERE v.deceased = 0 AND v.national_id IN (SELECT national_id FROM voters{where}){extra}"
        " ORDER BY agent IS NULL, agent COLLATE NOCASE, v.house_name COLLATE NOCASE, v.full_name COLLATE NOCASE",
        params).fetchall()
    supers = {}
    if kind == "super-agents":
        for nid, cid in db.execute("SELECT national_id, candidate_id FROM super_votes"):
            supers.setdefault(nid, []).append(names.get(cid, "?"))
    head = ["#", "ID", "Name", "Sex", "Age", "House", "Box", "Phone"]
    if kind == "super-agents":
        head.append("Super for")
    head += ["Agent", "Given by"]
    out = []
    for n, r in enumerate(rows, 1):
        line = [n, r["national_id"], r["full_name"], r["sex"], r["age"], r["house_name"], r["location"], r["phone"]]
        if kind == "super-agents":
            line.append(", ".join(sorted(supers.get(r["national_id"], []))))
        line += [r["agent"] or "No agent", ("One by one" if r["one_by_one"] else "House") if r["agent"] else ""]
        out.append(line)
    return head, out, f


@bp.route("/reports/list/<kind>")
@admin_required
def list_view(kind):
    known = lists()
    if kind not in known:
        abort(404)
    head, rows, f = list_rows(kind)
    per_agent = {}
    for line in rows:
        per_agent[line[-2]] = per_agent.get(line[-2], 0) + 1
    db = get_db()
    w, p = pledges._scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template("reports/list.html", kind=kind, title=known[kind][0], about=known[kind][1], head=head,
                           rows=rows[:500], total=len(rows), per_agent=sorted(per_agent.items(), key=lambda x: (x[0] == "No agent", -x[1])),
                           f=f, boxes=boxes, cands=pledges.candidates(), pc="", pv="", query_args=_filter_args())


@bp.route("/reports/list/<kind>/export.<fmt>")
@admin_required
def list_export(kind, fmt):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    from .voters import _safe_cell

    known = lists()
    if kind not in known or fmt not in ("csv", "xlsx"):
        abort(404)
    head, rows, f = list_rows(kind)
    rows = [[_safe_cell(x) if isinstance(x, str) else x for x in line] for line in rows]
    audit.log("report_list_export", target=kind, detail={"fmt": fmt, "rows": len(rows)})
    name = f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(head)
        w.writerows(rows)
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    wb = Workbook()
    ws = wb.active
    ws.title = known[kind][0][:31]
    ws.append(head)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for line in rows:
        ws.append(line)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
