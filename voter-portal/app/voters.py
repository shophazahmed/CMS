"""Voter search (all signed-in users) and editing / export (admins)."""
import csv
import io
import os
from datetime import datetime

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

from . import audit
from .auth import admin_required, is_admin, login_required
from .db import get_db, now_iso
from .normalize import clean_text, normalize_age, normalize_id, normalize_party, normalize_phone, normalize_sex

bp = Blueprint("voters", __name__)

PAGE_SIZE = 50
FIELDS = ("national_id", "full_name", "sex", "age", "house_name", "party", "phone")
SORTS = {
    "name": "full_name COLLATE NOCASE, national_id",
    "house": "house_name COLLATE NOCASE, full_name COLLATE NOCASE",
    "age": "age IS NULL, age, full_name COLLATE NOCASE",
    "id": "national_id",
    "party": "party, full_name COLLATE NOCASE",
}
MAX_PHOTO_BYTES = 5 * 1024 * 1024


def _like(text):
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def read_filters(args):
    def int_or_none(name):
        try:
            return max(0, min(130, int(args.get(name, ""))))
        except ValueError:
            return None

    return {
        "q": clean_text(args.get("q", ""))[:100],
        "national_id": clean_text(args.get("national_id", "")).upper().replace(" ", "")[:20],
        "house": clean_text(args.get("house", ""))[:100],
        "party": clean_text(args.get("party", ""))[:50],
        "sex": args.get("sex", "") if args.get("sex", "") in ("M", "F", "unknown") else "",
        "age_min": int_or_none("age_min"),
        "age_max": int_or_none("age_max"),
        "sort": args.get("sort", "name") if args.get("sort") in SORTS else "name",
    }


def build_where(f):
    clauses, params = [], []
    if f["q"]:
        clauses.append(
            "(full_name LIKE ? ESCAPE '\\' OR national_id LIKE ? ESCAPE '\\' OR house_name LIKE ? ESCAPE '\\'"
            " OR phone LIKE ? ESCAPE '\\')"
        )
        params += [_like(f["q"])] * 4
    if f["national_id"]:
        clauses.append("national_id LIKE ? ESCAPE '\\'")
        params.append(_like(f["national_id"]))
    if f["house"]:
        clauses.append("house_name LIKE ? ESCAPE '\\'")
        params.append(_like(f["house"]))
    if f["party"]:
        if f["party"] == "(none)":
            clauses.append("party = ''")
        else:
            clauses.append("party = ?")
            params.append(f["party"])
    if f["sex"] == "unknown":
        clauses.append("sex = ''")
    elif f["sex"]:
        clauses.append("sex = ?")
        params.append(f["sex"])
    if f["age_min"] is not None:
        clauses.append("age >= ?")
        params.append(f["age_min"])
    if f["age_max"] is not None:
        clauses.append("age <= ?")
        params.append(f["age_max"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def active_filters(f):
    return {k: v for k, v in f.items() if k != "sort" and v not in ("", None)}


@bp.route("/")
@login_required
def search():
    db = get_db()
    f = read_filters(request.args)
    where, params = build_where(f)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = db.execute(f"SELECT COUNT(*) FROM voters{where}", params).fetchone()[0]
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    rows = db.execute(
        f"SELECT * FROM voters{where} ORDER BY {SORTS[f['sort']]} LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    parties = [r[0] for r in db.execute("SELECT DISTINCT party FROM voters WHERE party != '' ORDER BY party")]
    houses = [r[0] for r in db.execute(
        "SELECT DISTINCT house_name FROM voters WHERE house_name != '' ORDER BY house_name COLLATE NOCASE"
    )]
    stats = db.execute(
        "SELECT COUNT(*) AS total, SUM(sex = 'M') AS male, SUM(sex = 'F') AS female FROM voters"
    ).fetchone()
    query_args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template(
        "voters/list.html",
        rows=rows,
        f=f,
        filtered=bool(active_filters(f)),
        total=total,
        page=page,
        pages=pages,
        parties=parties,
        houses=houses,
        stats=stats,
        query_args=query_args,
    )


def get_voter_or_404(national_id):
    row = get_db().execute("SELECT * FROM voters WHERE national_id = ?", (national_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/voters/<national_id>")
@login_required
def detail(national_id):
    voter = get_voter_or_404(national_id)
    editor = None
    if voter["updated_by"] and is_admin():
        editor = get_db().execute("SELECT username FROM users WHERE id = ?", (voter["updated_by"],)).fetchone()
    return render_template("voters/detail.html", v=voter, editor=editor)


@bp.route("/photos/<national_id>")
@login_required
def photo(national_id):
    voter = get_voter_or_404(national_id)
    if not voter["photo_path"]:
        abort(404)
    resp = send_from_directory(current_app.config["PHOTOS_DIR"], voter["photo_path"], max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# --- admin: add / edit / delete --------------------------------------------

def _parse_form(form, creating):
    errors, data = [], {}
    if creating:
        nid, note = normalize_id(form.get("national_id", ""))
        if nid is None or (note and "unusual" in note):
            errors.append("ID card must look like A123456.")
        data["national_id"] = nid
    data["full_name"] = clean_text(form.get("full_name", ""))[:200]
    if not data["full_name"]:
        errors.append("Full name is required.")
    data["sex"], note = normalize_sex(form.get("sex", ""))
    if note:
        errors.append("Sex must be M, F or blank.")
    data["age"], note = normalize_age(form.get("age", ""))
    if note:
        errors.append("Age must be a number from 0 to 130.")
    data["house_name"] = clean_text(form.get("house_name", ""))[:200]
    data["party"] = normalize_party(form.get("party", ""))[:50]
    data["phone"], note = normalize_phone(form.get("phone", ""))
    if note and "unreadable" in note:
        errors.append("Phone must be a number such as 7771234.")
    return data, errors


def _save_photo(upload, national_id):
    """Validate an uploaded image and store it re-encoded as JPEG (drops EXIF/GPS)."""
    from PIL import Image, UnidentifiedImageError

    blob = upload.read(MAX_PHOTO_BYTES + 1)
    if len(blob) > MAX_PHOTO_BYTES:
        return None, "Photo must be 5 MB or smaller."
    try:
        img = Image.open(io.BytesIO(blob))
        if img.format not in ("JPEG", "PNG", "WEBP"):
            return None, "Photo must be a JPEG, PNG or WebP image."
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return None, "That file isn't a readable image."
    img = img.convert("RGB")
    img.thumbnail((800, 800))
    name = f"{national_id}.jpg"
    path = os.path.join(current_app.config["PHOTOS_DIR"], name)
    img.save(path, "JPEG", quality=85)
    return name, None


@bp.route("/voters/new", methods=["GET", "POST"])
@admin_required
def create():
    if request.method == "POST":
        data, errors = _parse_form(request.form, creating=True)
        db = get_db()
        if not errors and db.execute(
            "SELECT 1 FROM voters WHERE national_id = ?", (data["national_id"],)
        ).fetchone():
            errors.append(f"A voter with ID {data['national_id']} already exists.")
        photo = request.files.get("photo")
        if not errors and photo and photo.filename:
            data["photo_path"], err = _save_photo(photo, data["national_id"])
            if err:
                errors.append(err)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("voters/form.html", v=request.form, creating=True), 400
        stamp = now_iso()
        db.execute(
            "INSERT INTO voters (national_id, full_name, sex, age, house_name, party, phone, photo_path,"
            " source, created_at, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?)",
            (data["national_id"], data["full_name"], data["sex"], data["age"], data["house_name"],
             data["party"], data["phone"], data.get("photo_path"), stamp, stamp, g.user["id"]),
        )
        audit.log("voter_create", target=data["national_id"], detail={k: data[k] for k in FIELDS}, commit=False)
        db.commit()
        flash("Voter added.", "success")
        return redirect(url_for("voters.detail", national_id=data["national_id"]))
    return render_template("voters/form.html", v={}, creating=True)


@bp.route("/voters/<national_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(national_id):
    voter = get_voter_or_404(national_id)
    if request.method == "POST":
        data, errors = _parse_form(request.form, creating=False)
        photo = request.files.get("photo")
        if not errors and photo and photo.filename:
            data["photo_path"], err = _save_photo(photo, national_id)
            if err:
                errors.append(err)
        if request.form.get("remove_photo") and not data.get("photo_path"):
            data["photo_path"] = None
        if errors:
            for e in errors:
                flash(e, "error")
            merged = dict(voter) | request.form.to_dict()
            return render_template("voters/form.html", v=merged, creating=False), 400
        changes = {
            k: {"from": voter[k], "to": data[k]}
            for k in list(FIELDS[1:]) + ["photo_path"]
            if k in data and voter[k] != data[k]
        }
        if changes:
            db = get_db()
            sets = ", ".join(f"{k} = ?" for k in changes)
            db.execute(
                f"UPDATE voters SET {sets}, updated_at = ?, updated_by = ? WHERE national_id = ?",
                [data[k] for k in changes] + [now_iso(), g.user["id"], national_id],
            )
            audit.log("voter_update", target=national_id, detail=changes, commit=False)
            db.commit()
            flash("Changes saved.", "success")
        else:
            flash("Nothing changed.", "info")
        return redirect(url_for("voters.detail", national_id=national_id))
    return render_template("voters/form.html", v=voter, creating=False)


@bp.post("/voters/<national_id>/delete")
@admin_required
def delete(national_id):
    voter = get_voter_or_404(national_id)
    db = get_db()
    db.execute("DELETE FROM voters WHERE national_id = ?", (national_id,))
    audit.log("voter_delete", target=national_id, detail={k: voter[k] for k in FIELDS}, commit=False)
    db.commit()
    flash(f"Voter {national_id} deleted.", "success")
    return redirect(url_for("voters.search"))


# --- admin: export ----------------------------------------------------------

EXPORT_HEADERS = ["ID", "Full name", "Sex", "Age", "House name", "Party", "Phone", "Has photo"]


def _safe_cell(value):
    """Stop spreadsheet formula injection (=, +, -, @ at the start of a cell)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@bp.route("/export.<fmt>")
@admin_required
def export(fmt):
    if fmt not in ("csv", "xlsx"):
        abort(404)
    f = read_filters(request.args)
    where, params = build_where(f)
    rows = get_db().execute(f"SELECT * FROM voters{where} ORDER BY {SORTS[f['sort']]}", params).fetchall()
    table = [
        [_safe_cell(r["national_id"]), _safe_cell(r["full_name"]), r["sex"], r["age"],
         _safe_cell(r["house_name"]), _safe_cell(r["party"]), r["phone"], "yes" if r["photo_path"] else ""]
        for r in rows
    ]
    audit.log("export", target=fmt, detail={"filters": active_filters(f), "rows": len(rows)})
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"voters-{stamp}.{fmt}"

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(EXPORT_HEADERS)
        writer.writerows(table)
        # BOM so Excel opens UTF-8 (Thaana) text correctly
        return Response(
            "﻿" + buf.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Voters"
    ws.append(EXPORT_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in table:
        ws.append(row)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col, width in zip("ABCDEFGH", (11, 32, 6, 6, 28, 14, 12, 10)):
        ws.column_dimensions[col].width = width
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return send_file(
        out,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
