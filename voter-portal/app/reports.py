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
        dims["pattern"] = ("Pledge pattern",
                           "(SELECT CASE COUNT(*) WHEN 0 THEN 'No Yes' WHEN 1 THEN 'Only ' || MAX(c.name)"
                           " ELSE 'Shared: ' || group_concat(c.name, ' & ') END FROM pledges p JOIN candidates c"
                           " ON c.id = p.candidate_id AND c.active = 1 WHERE p.national_id = v.national_id AND p.value = 'yes')",
                           (), None, [f"Only {c['name']}" for c in cands])
        from .auth import can_view_super

        if can_view_super():
            dims["super"] = ("Super pledge for", "COALESCE(cs.name, 'Not a super voter')", ("super",), None,
                             [c["name"] for c in cands] + ["Not a super voter"])
            for c in cands:
                dims[f"s{c['id']}"] = (f"Super: {c['name']}",
                                       "CASE WHEN v.national_id IN (SELECT national_id FROM super_votes WHERE candidate_id ="
                                       f" {int(c['id'])}) THEN 'Super voter' ELSE 'Not super' END", (), None,
                                       ["Super voter", "Not super"])
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
    "super": (" LEFT JOIN super_votes sv ON sv.national_id = v.national_id"
              " LEFT JOIN candidates cs ON cs.id = sv.candidate_id AND cs.active = 1"),
}

# Candidate reports: (key, title, rows, column prefix) -> e.g. "Sofhaz: pledges by box"
CAND_REPORTS = [("p-box", "pledges by box", "box", "p"), ("p-agent", "pledges by agent", "agent", "p"),
                ("p-age", "pledges by age group", "age", "p"), ("p-sex", "pledges by sex", "sex", "p"),
                ("p-house", "pledges by house", "house", "p"),
                ("s-box", "super voters by box", "box", "s"), ("s-agent", "super voters by agent", "agent", "s"),
                ("s-age", "super voters by age group", "age", "s")]


def resolve():
    """(title, rows, cols, preset) for this request: a default report (?r=), a candidate report (?cand=&what=)
    or your own (?rows=&cols=)."""
    key = request.args.get("r", "")
    preset = next((p for p in presets() if p[0] == key), None)
    if preset:
        return preset[1], preset[3], preset[4], preset
    cand, what = request.args.get("cand", ""), request.args.get("what", "")
    if cand.isdigit() and what:
        c = next((c for c in pledges.candidates() if str(c["id"]) == cand), None)
        rep = next((r for r in CAND_REPORTS if r[0] == what), None)
        if c is None or rep is None or f"{rep[3]}{cand}" not in dimensions():
            abort(400)
        return f"{c['name']}: {rep[1]}", rep[2], f"{rep[3]}{cand}", None
    dims = dimensions()
    rows_key, cols_key = request.args.get("rows", "box"), request.args.get("cols", "")
    title = dims.get(rows_key, ("Report",))[0] + (f" by {dims[cols_key][0]}" if cols_key in dims else "")
    return title, rows_key, cols_key, None


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
    multi = bool({"yes", "super"} & {rows_key, cols_key})  # a voter can be in two columns
    count = "COUNT(*)" if multi else "COUNT(DISTINCT v.national_id)"
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
            "max": max(cells.values(), default=0), "pledge_rows": multi}


def _filter_args():
    return {k: v for k, v in request.args.items() if k not in ("rows", "cols", "pct", "r", "page", "cand", "what")}


def _boxes():
    w, p = pledges._scoped_where(" WHERE location != ''")
    return [r[0] for r in get_db().execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]


@bp.route("/reports")
@admin_required
def index():
    dims = dimensions()
    f, *_ = pledges._filters()
    boxes = _boxes()
    from .auth import can_view_super

    return render_template("reports/index.html", presets=presets(), dims=dims, f=f, boxes=boxes, lists=lists(),
                           cands=pledges.candidates(), pc="", pv="", query_args=_filter_args(),
                           agents=pledges.agent_options(), cand_reports=[r for r in CAND_REPORTS
                                                                         if r[3] == "p" or can_view_super()],
                           show_cands=can_view_pledges())


def drill(dim, value, agent_ids):
    """The Voters list filters for one value of a report column / row, or None when there's no such filter."""
    if dim == "box":
        return {"loc": "(none)" if value == NONE else value}
    if dim == "house":
        return None if value == NONE else {"h": value}
    if dim == "sex":
        return {"sex": {"Men": "M", "Women": "F"}.get(value, "unknown")}
    if dim == "agent":
        return {"ag": "none"} if value == "No agent" else ({"ag": str(agent_ids[value])} if value in agent_ids else None)
    if dim.startswith("p") and dim[1:].isdigit():
        return {"pc": dim[1:], "pv": value or "none"}
    if dim == "noshow":
        return {"ns": "1" if value == "No-show" else "0"}
    if dim == "yes":
        cands = {c["name"]: c["id"] for c in pledges.candidates()}
        return {"pc": "any", "pv": "none"} if value == "No Yes" else (
            {"pc": str(cands[value]), "pv": "yes"} if value in cands else None)
    if dim == "pattern":
        cands = {c["name"]: c["id"] for c in pledges.candidates()}
        if value == "No Yes":
            return {"pc": "any", "pv": "none"}
        if value.startswith("Only ") and value[5:] in cands:
            return {"pp": f"only:{cands[value[5:]]}"}
        if value.startswith("Shared: "):
            pair = value[8:].split(" & ")
            if len(pair) == 2 and all(n in cands for n in pair):
                a, b = sorted(cands[n] for n in pair)
                return {"pp": f"both:{a}-{b}"}
    return None


def drill_links(t, rows_key, cols_key):
    """URLs to the Voters list for each cell, row and column of a report (where filters exist for them)."""
    from flask import url_for

    agent_ids = {a["username"]: a["id"] for a in pledges.agent_options()}
    base = _filter_args()
    rd = {r: drill(rows_key, r, agent_ids) for r, _ in t["rows"]}
    cd = {c: drill(cols_key, c, agent_ids) if cols_key else {} for c, _ in t["cols"]}

    def url(*parts):
        merged = dict(base)
        keys = set()
        for part in parts:
            if part is None or keys & set(part):
                return None
            keys |= set(part)
            merged.update(part)
        return url_for("reports.list_view", kind="voters", **merged)

    return ({(r, c): url(rd[r], cd[c]) for r, _ in t["rows"] for c, _ in t["cols"]},
            {r: url(rd[r]) for r, _ in t["rows"]}, {c: url(cd[c]) for c, _ in t["cols"]})


@bp.route("/reports/view")
@admin_required
def view():
    title, rows_key, cols_key, preset = resolve()
    t = build(rows_key, cols_key, request.args)
    cell_links, row_links, col_links = drill_links(t, rows_key, cols_key)
    return render_template("reports/view.html", t=t, preset=preset, title=title, rows_key=rows_key, cols_key=cols_key,
                           cell_links=cell_links, row_links=row_links, col_links=col_links,
                           dims=dimensions(), pct=request.args.get("pct", ""), key=request.args.get("r", ""),
                           cand=request.args.get("cand", ""), what=request.args.get("what", ""), boxes=_boxes(),
                           agents=pledges.agent_options(), f=t["f"], cands=pledges.candidates(), pc="", pv="",
                           query_args=_filter_args())


def candidate_overview():
    """Per candidate: Yes / Maybe / No / not marked, solid base, shared, super voters, Yes expected to vote."""
    f, where, params, cands, *_ = pledges._filters()
    db = get_db()
    sub = f"SELECT national_id FROM voters{where}"
    total = db.execute(f"SELECT COUNT(*) FROM voters WHERE deceased = 0 AND national_id IN ({sub})", params).fetchone()[0]
    ids = [c["id"] for c in cands]
    marks = {}
    for cid, value, n in db.execute(f"SELECT candidate_id, value, COUNT(*) FROM pledges WHERE national_id IN ({sub})"
                                    " GROUP BY candidate_id, value", params):
        marks[(cid, value)] = n
    solid = dict(db.execute(
        f"SELECT MAX(candidate_id), COUNT(*) FROM (SELECT national_id, MAX(candidate_id) AS candidate_id FROM pledges"
        f" WHERE value = 'yes' AND candidate_id IN ({','.join('?' * len(ids)) or 'NULL'}) AND national_id IN ({sub})"
        " GROUP BY national_id HAVING COUNT(*) = 1) GROUP BY candidate_id", ids + params).fetchall()) if ids else {}
    from .auth import can_view_super

    supers = dict(db.execute(f"SELECT candidate_id, COUNT(*) FROM super_votes WHERE national_id IN ({sub})"
                             " GROUP BY candidate_id", params).fetchall()) if can_view_super() else None
    expected = dict(db.execute(f"SELECT candidate_id, COUNT(*) FROM pledges WHERE value = 'yes' AND national_id IN ({sub})"
                               " AND national_id NOT IN (SELECT national_id FROM pledge_noshow) GROUP BY candidate_id",
                               params).fetchall())
    head = ["Candidate", "Yes", "Maybe", "No", "Not marked", "Solid base (only them)", "Shared (Yes for 2)",
            "Expected to vote (Yes, not no-show)"] + (["Super voters"] if supers is not None else [])
    rows = []
    for c in cands:
        yes, maybe, no = (marks.get((c["id"], v), 0) for v in ("yes", "maybe", "no"))
        line = [pledges.label(c), yes, maybe, no, total - yes - maybe - no, solid.get(c["id"], 0),
                yes - solid.get(c["id"], 0), expected.get(c["id"], 0)]
        if supers is not None:
            line.append(supers.get(c["id"], 0))
        rows.append(line)
    return head, rows, total, f


@bp.route("/reports/candidates")
@admin_required
def candidates():
    if not can_view_pledges():
        abort(404)
    head, rows, total, f = candidate_overview()
    top = max([x for line in rows for x in line[1:]] or [0])
    return render_template("reports/candidates.html", head=head, rows=rows, total=total, top=top, f=f,
                           boxes=_boxes(), agents=pledges.agent_options(), cands=pledges.candidates(), pc="", pv="",
                           query_args=_filter_args(), ours=pledges.our_candidate(), cids=[c["id"] for c in pledges.candidates()])


@bp.route("/reports/candidates/export.<fmt>")
@admin_required
def candidates_export(fmt):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    if not can_view_pledges() or fmt not in ("csv", "xlsx"):
        abort(404)
    head, rows, total, f = candidate_overview()
    audit.log("report_export", target=fmt, detail={"reports": ["Candidates overview"]})
    name = f"candidates-{datetime.now().strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(head)
        w.writerows(rows)
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    wb = Workbook()
    ws = wb.active
    ws.title = "Candidates"
    ws.append([f"Candidates overview · {total} voters"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(head)
    for cell in ws[2]:
        cell.font = Font(bold=True)
    for line in rows:
        ws.append(line)
    ws.column_dimensions["A"].width = 24
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


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
        title, rows_key, cols_key, _ = resolve()
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

    out = {"voters": ("Voters list", "Every voter matching the filters, with their canvassing status, pledges,"
                                     " super pledge and contact before the vote (as far as you may see them) and agent."),
           "voters-agents": ("Voters and their agent", "Every voter with the agent who looks after them (one by one, or"
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
    from .auth import can_view_super
    from .warroom import CONTACT

    full = kind == "voters"
    show_pledges = full and can_view_pledges()
    show_super = kind == "super-agents" or (full and can_view_super() and can_view_pledges())
    show_contact = full and _war_ok()
    supers, marks, noshow, contact, status = {}, {}, set(), {}, {}
    if show_super:
        for nid, cid in db.execute("SELECT national_id, candidate_id FROM super_votes"):
            supers.setdefault(nid, []).append(names.get(cid, "?"))
    if show_pledges:
        for nid, cid, value in db.execute("SELECT national_id, candidate_id, value FROM pledges"):
            marks[(nid, cid)] = value
        noshow = {r[0] for r in db.execute("SELECT national_id FROM pledge_noshow")}
    if show_contact:
        contact = dict(db.execute("SELECT national_id, status FROM war_contact").fetchall())
    if full:
        status = dict(db.execute(f"SELECT national_id, status FROM voters WHERE national_id IN (SELECT national_id FROM voters{where})",
                                 params).fetchall())
    head = ["#", "ID", "Name", "Sex", "Age", "House", "Box", "Phone"]
    if full:
        head.append("Canvassing")
    if show_pledges:
        head += [c["name"] for c in cands] + ["No-show"]
    if show_super:
        head.append("Super for")
    if show_contact:
        head.append("Said before the vote")
    head += ["Agent", "Given by"]
    values = {"yes": "Yes", "maybe": "Maybe", "no": "No"}
    out = []
    for n, r in enumerate(rows, 1):
        nid = r["national_id"]
        line = [n, nid, r["full_name"], r["sex"], r["age"], r["house_name"], r["location"], r["phone"]]
        if full:
            line.append(STATUSES.get(status.get(nid), ""))
        if show_pledges:
            line += [values.get(marks.get((nid, c["id"])), "") for c in cands] + ["No-show" if nid in noshow else ""]
        if show_super:
            line.append(", ".join(sorted(supers.get(nid, []))))
        if show_contact:
            line.append(CONTACT.get(contact.get(nid), "Not reached yet"))
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
    ids = {a["username"]: a["id"] for a in pledges.agent_options()}
    db = get_db()
    w, p = pledges._scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template("reports/list.html", kind=kind, title=known[kind][0], about=known[kind][1], head=head,
                           rows=rows[:500], total=len(rows), per_agent=sorted(per_agent.items(), key=lambda x: (x[0] == "No agent", -x[1])),
                           f=f, boxes=boxes, cands=pledges.candidates(), pc="", pv="", query_args=_filter_args(),
                           agent_ids=ids, agents=pledges.agent_options())


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
