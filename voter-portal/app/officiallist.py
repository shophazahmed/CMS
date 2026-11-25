"""The official voter lists (Elections Commission), one file per ballot box.

Each file is the box's printed list as Excel: a "BALLOT BOX" code (e.g. LOC-GDH-GADHDHOO), the box name and
voter count, then pages of rows: No., Name, Island, House, Constituency, AR, YW. There is no ID card number,
so voters are matched to the portal by name and house (then by a unique name, then by a close spelling in
the same house). For each match the voter's list number is saved for the War Room (box agents type the
number the official calls out) and, if chosen, their box is set from the list. "Check only" shows the
result without saving anything; the voters that couldn't be matched are listed and can be downloaded.
"""
import csv
import difflib
import io
import re

from flask import Blueprint, Response, abort, g, render_template, request

from . import audit
from .auth import is_owner, login_required
from .db import get_db
from .warroom import tier

bp = Blueprint("officiallist", __name__)

MAX_BYTES = 20 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS war_official_unmatched (
    user_id INTEGER NOT NULL,
    box     TEXT NOT NULL,
    list_no INTEGER,
    name    TEXT NOT NULL,
    island  TEXT NOT NULL DEFAULT '',
    house   TEXT NOT NULL DEFAULT ''
);
"""


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def _rows_of(upload):
    """All rows of every sheet as lists of strings, from .xls / .xlsx / .csv."""
    blob = upload.read(MAX_BYTES + 1)
    if len(blob) > MAX_BYTES:
        raise ValueError(f"{upload.filename}: the file must be 20 MB or smaller.")
    name = (upload.filename or "").lower()
    sheets = []
    try:
        if name.endswith(".xls"):
            import xlrd

            wb = xlrd.open_workbook(file_contents=blob)
            for sh in wb.sheets():
                sheets.append([[_cell(v) for v in sh.row_values(r)] for r in range(sh.nrows)])
        elif name.endswith((".xlsx", ".xlsm")):
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
            for ws in wb.worksheets:
                sheets.append([[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)])
        elif name.endswith(".csv"):
            sheets.append([[c.strip() for c in row] for row in csv.reader(io.StringIO(blob.decode("utf-8-sig", "replace")))])
        else:
            raise ValueError(f"{upload.filename}: use the Excel file (.xls or .xlsx) or a CSV.")
    except ValueError:
        raise
    except Exception:
        raise ValueError(f"{upload.filename}: the portal couldn't read this file.")
    return sheets


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse(upload):
    """{code, box, expected, rows: [(no, name, island, house)]} from one official list file."""
    code = box = None
    expected = None
    rows = []
    for sheet in _rows_of(upload):
        cols = None
        for r, vals in enumerate(sheet):
            low = [v.lower() for v in vals]
            for v in vals:
                if code is None and re.fullmatch(r"LOC-[A-Z0-9-]+", v):
                    code = v
                    for nxt in sheet[r + 1:r + 4]:
                        parts = [p for p in nxt if p]
                        if parts:
                            box = " ".join(parts)
                            break
                m = re.fullmatch(r"(\d+) voters", v)
                if m and expected is None:
                    expected = int(m.group(1))
            if "no." in low and "name" in low and "house" in low:
                cols = {k: low.index(k) for k in ("no.", "name", "house")}
                cols["island"] = low.index("island") if "island" in low else None
                continue
            if cols and len(vals) > max(c for c in cols.values() if c is not None):
                no = vals[cols["no."]]
                if no.isdigit() and vals[cols["name"]]:
                    rows.append((int(no), vals[cols["name"]], vals[cols["island"]] if cols["island"] is not None else "",
                                 vals[cols["house"]]))
    if not rows:
        raise ValueError(f"{upload.filename}: no voter rows found (it needs No., Name and House columns).")
    if not box:
        box = code or upload.filename
    return {"code": code or "", "box": box, "expected": expected, "rows": rows, "file": upload.filename}


def match(rows):
    """[(row, national_id or None, how)] matching official rows to portal voters."""
    db = get_db()
    voters = db.execute("SELECT national_id, full_name, house_name FROM voters WHERE deceased = 0").fetchall()
    by_both, by_name, by_house = {}, {}, {}
    for v in voters:
        n, h = _norm(v["full_name"]), _norm(v["house_name"])
        by_both.setdefault((n, h), []).append(v["national_id"])
        by_name.setdefault(n, []).append(v["national_id"])
        by_house.setdefault(h, []).append((n, v["national_id"]))
    used, out = set(), []
    for row in rows:
        no, name, island, house = row
        n, h = _norm(name), _norm(house)
        hit, how = None, ""
        cands = [x for x in by_both.get((n, h), []) if x not in used]
        if len(cands) == 1:
            hit, how = cands[0], "name and house"
        if hit is None:
            cands = [x for x in by_name.get(n, []) if x not in used]
            if len(cands) == 1:
                hit, how = cands[0], "name only (house differs)"
        if hit is None and by_house.get(h):
            scored = sorted(((difflib.SequenceMatcher(None, n, vn).ratio(), nid) for vn, nid in by_house[h]
                             if nid not in used), reverse=True)
            if scored and scored[0][0] >= 0.88 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05):
                hit, how = scored[0][1], "close spelling, same house"
        if hit is not None:
            used.add(hit)
        out.append((row, hit, how))
    return out


@bp.route("/war/official", methods=["GET", "POST"])
@login_required
def official():
    """Upload the official lists (one or more files): check, then save."""
    if not (is_owner() or g.user["role"] == "admin" or tier() in ("owner", "room")):
        abort(403)
    if request.method == "GET":
        return render_template("warroom/official.html", result=None)
    files = [f for f in request.files.getlist("lists") if f and f.filename]
    if not files:
        return render_template("warroom/official.html", result=None, error="Choose the official list file(s).")
    apply = request.form.get("apply") == "1"
    set_box = request.form.get("set_box") == "1"
    boxes, unmatched, errors = [], [], []
    db = get_db()
    for f in files:
        try:
            parsed = parse(f)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        matched = match(parsed["rows"])
        hows = {}
        for row, nid, how in matched:
            if nid is None:
                unmatched.append((parsed["box"],) + row)
                continue
            hows[how] = hows.get(how, 0) + 1
            if apply:
                if set_box:
                    db.execute("UPDATE voters SET location = ? WHERE national_id = ?", (parsed["box"], nid))
                db.execute("INSERT INTO war_list (national_id, list_no, box) VALUES (?, ?, ?) ON CONFLICT(national_id)"
                           " DO UPDATE SET list_no = excluded.list_no, box = excluded.box", (nid, row[0], parsed["box"]))
        boxes.append({"box": parsed["box"], "code": parsed["code"], "file": parsed["file"], "expected": parsed["expected"],
                      "rows": len(parsed["rows"]), "matched": sum(1 for _, nid, _ in matched if nid), "hows": hows,
                      "complete": parsed["expected"] is None or parsed["expected"] == len(parsed["rows"])})
    if apply and boxes:
        audit.log("war_official_lists", detail={"boxes": [(b["box"], b["matched"], b["rows"]) for b in boxes],
                                                "set_box": set_box}, commit=False)
        db.commit()
    # keep the last check's unmatched rows for this person's download
    db.execute("DELETE FROM war_official_unmatched WHERE user_id = ?", (g.user["id"],))
    db.executemany("INSERT INTO war_official_unmatched (user_id, box, list_no, name, island, house) VALUES (?, ?, ?, ?, ?, ?)",
                   [(g.user["id"],) + tuple(u) for u in unmatched])
    db.commit()
    return render_template("warroom/official.html", result={"boxes": boxes, "unmatched": unmatched, "errors": errors,
                                                            "applied": apply, "set_box": set_box})


@bp.route("/war/official/unmatched.csv")
@login_required
def unmatched_csv():
    if not (is_owner() or g.user["role"] == "admin" or tier() in ("owner", "room")):
        abort(403)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Box", "No.", "Name", "Island", "House"])
    for line in get_db().execute("SELECT box, list_no, name, island, house FROM war_official_unmatched WHERE user_id = ?"
                                 " ORDER BY box, list_no", (g.user["id"],)).fetchall():
        line = list(line)
        w.writerow([("'" + x) if isinstance(x, str) and x[:1] in "=+-@" else x for x in line])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="official-list-not-matched.csv"'})
