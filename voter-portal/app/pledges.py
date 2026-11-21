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
from .auth import (can_mark_pledges, is_owner, pledge_marker_required, pledge_owner_required,
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
CREATE TABLE IF NOT EXISTS pledge_noshow (
    national_id TEXT PRIMARY KEY,
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
    conn.executescript(SCHEMA)
    conn.commit()


def candidates(active_only=True):
    cond = " WHERE active = 1" if active_only else ""
    return get_db().execute(f"SELECT * FROM candidates{cond} ORDER BY position, name COLLATE NOCASE").fetchall()


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
    pv = pv if pv in ("yes", "maybe", "no", "none") else ""
    if pc and pv:
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
    return f, where, params, cands, pc, pv


# Sortable columns on the sheet: (A to Z, Z to A); empty values always go last
SHEET_SORTS = {
    "name": ("full_name COLLATE NOCASE, national_id", "full_name COLLATE NOCASE DESC, national_id"),
    "house": ("house_name = '', house_name COLLATE NOCASE, full_name COLLATE NOCASE",
              "house_name = '', house_name COLLATE NOCASE DESC, full_name COLLATE NOCASE"),
    "box": ("location = '', location COLLATE NOCASE, house_name COLLATE NOCASE, full_name COLLATE NOCASE",
            "location = '', location COLLATE NOCASE DESC, house_name COLLATE NOCASE, full_name COLLATE NOCASE"),
    "age": ("age IS NULL, age, full_name COLLATE NOCASE", "age IS NULL, age DESC, full_name COLLATE NOCASE"),
    "id": ("national_id", "national_id DESC"),
}


def sheet_order():
    """(key, direction, ORDER BY) from ?sort=name|house|box|age|id&dir=asc|desc (default: name, A to Z)."""
    key = request.args.get("sort", "name")
    key = key if key in SHEET_SORTS else "name"
    direction = "desc" if request.args.get("dir") == "desc" else "asc"
    return key, direction, SHEET_SORTS[key][1 if direction == "desc" else 0]


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
                           noshows=db.execute(f"SELECT COUNT(*) FROM pledge_noshow WHERE national_id IN"
                                              f" (SELECT national_id FROM voters{where})", params).fetchone()[0],
                           query_args=query_args, first=(page - 1) * size)


def _scoped_where(sql="", params=()):
    from .voters import scoped

    return scoped(sql, params)


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
        audit.log("pledge_noshow", target=nid, detail={"noshow": wanted == "1"}, commit=False)
        db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "noshow": wanted == "1"}
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/") else url_for("pledges.sheet"))


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
                if name and not db.execute("SELECT 1 FROM candidates WHERE name = ? AND id != ?", (name, cid)).fetchone():
                    db.execute("UPDATE candidates SET name = ?, color = ? WHERE id = ?", (name, color, cid))
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
                           max_votes=max_votes())


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
    headers = (["#", "ID", "Full name", "Sex", "Age", "House name", "Party", "Phone", "Box", "No-show"]
               + [c["name"] for c in cands])
    table = []
    for n, r in enumerate(rows, 1):
        mine = marks.get(r["national_id"], {})
        table.append([n, r["national_id"], _safe_cell(r["full_name"]), r["sex"], r["age"], _safe_cell(r["house_name"]),
                      _safe_cell(r["party"]), r["phone"], _safe_cell(r["location"]),
                      "yes" if r["national_id"] in away else ""]
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
    return {"total": total, "not_pledged": total - anyone, "noshows": away,
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
    box_list = sorted(box_total, key=lambda b: (-box_total[b], b or "~"))

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
        expected=expected, boxes=boxes, box_total=box_total,
        box_list=box_list, group_rows=group_rows, trend=trend, trend_max=trend_max, f=f, box_options=box_options,
        query_args={k: v for k, v in request.args.items()})
