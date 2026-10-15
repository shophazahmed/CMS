"""Voter search (all signed-in users) and editing / export (admins)."""
import csv
import io
import os
from datetime import datetime, timedelta, timezone

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

from . import audit, campaign
from .auth import admin_required, is_admin, is_owner, login_required
from .db import get_db, now_iso
from .normalize import clean_text, normalize_age, normalize_id, normalize_party, normalize_phone, normalize_sex

bp = Blueprint("voters", __name__)

PAGE_SIZE = 50
PAGE_SIZES = (25, 50, 100, 200)
FIELDS = ("national_id", "full_name", "sex", "age", "house_name", "party", "phone")
SORTS = {
    "name": "full_name COLLATE NOCASE, national_id",
    "house": "house_name COLLATE NOCASE, full_name COLLATE NOCASE",
    "age": "age IS NULL, age, full_name COLLATE NOCASE",
    "id": "national_id",
    "party": "party, full_name COLLATE NOCASE",
}
MAX_PHOTO_BYTES = 5 * 1024 * 1024
STATUSES = {"new": "Not contacted", "contacted": "Contacted", "followup": "Follow up", "noanswer": "No answer"}
MAX_NOTES = 2000


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
        "h": clean_text(args.get("h", ""))[:200],
        "party": clean_text(args.get("party", ""))[:50],
        "sex": args.get("sex", "") if args.get("sex", "") in ("M", "F", "unknown") else "",
        "age_min": int_or_none("age_min"),
        "age_max": int_or_none("age_max"),
        "status": args.get("status", "") if args.get("status", "") in STATUSES else "",
        # Individually assigned: an agent's id, or "any"
        # Who last set the status (someone who reached the voter): a user id
        "by": args.get("by", "") if args.get("by", "").isdigit() else "",
        # Responsible agent (house or one by one): an agent's id, or "none"
        "ag": args.get("ag", "") if args.get("ag", "") == "none" or args.get("ag", "").isdigit() else "",
        # 100% confirmed (1) / not (0): only the 'admin' account may filter on it
        "cf": args.get("cf", "") if args.get("cf", "") in ("0", "1") and is_owner() else "",
        "va": args.get("va", "") if args.get("va", "") == "any" or args.get("va", "").isdigit() else "",
        "sort": args.get("sort", "name") if args.get("sort") in SORTS else "name",
    }


def handled_by(placeholder="?"):
    """Voters an agent is responsible for: handed to them one by one, or in their house and not handed
    to anyone else individually."""
    return (f"(national_id IN (SELECT national_id FROM voter_assignments WHERE user_id = {placeholder})"
            f" OR (house_name IN (SELECT house_name FROM house_assignments WHERE user_id = {placeholder})"
            " AND national_id NOT IN (SELECT national_id FROM voter_assignments)))")


NO_AGENT = ("(national_id NOT IN (SELECT national_id FROM voter_assignments)"
            " AND house_name NOT IN (SELECT house_name FROM house_assignments))")


def agent_scope():
    """SQL limiting an agent to their voters (None for other roles): the ones handed to them one by one,
    plus everyone in their houses who hasn't been handed to someone individually."""
    user = g.get("user")
    if user is not None and user["role"] == "agent":
        return handled_by(), [user["id"], user["id"]]
    return None, []


def scoped(sql_where="", params=()):
    """Add the access scope (agent's houses, campaign parties) to a WHERE clause ('' or ' WHERE ...')."""
    params = list(params)
    for clause, extra in (agent_scope(), campaign.party_scope()):
        if not clause:
            continue
        sql_where = f"{sql_where} AND {clause}" if sql_where else f" WHERE {clause}"
        params += extra
    return sql_where, params


def build_where(f):
    clauses, params = [], []
    if f.get("h"):
        clauses.append("house_name = ?")
        params.append(f["h"])
    if f["q"]:
        clauses.append(
            "(full_name LIKE ? ESCAPE '\\' OR national_id LIKE ? ESCAPE '\\' OR house_name LIKE ? ESCAPE '\\'"
            " OR phone LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\')"
        )
        params += [_like(f["q"])] * 5
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
    if f.get("status"):
        clauses.append("status = ?")
        params.append(f["status"])
    if f.get("by"):
        clauses.append("status_by = ? AND status != 'new'")
        params.append(int(f["by"]))
    if f.get("cf") == "1":
        clauses.append("national_id IN (SELECT national_id FROM confirmed_voters)")
    elif f.get("cf") == "0":
        clauses.append("national_id NOT IN (SELECT national_id FROM confirmed_voters)")
    if f.get("ag") == "none":
        clauses.append(NO_AGENT)
    elif f.get("ag"):
        clauses.append(handled_by())
        params += [int(f["ag"])] * 2
    if f.get("va") == "any":
        clauses.append("national_id IN (SELECT national_id FROM voter_assignments)")
    elif f.get("va"):
        clauses.append("national_id IN (SELECT national_id FROM voter_assignments WHERE user_id = ?)")
        params.append(int(f["va"]))
    return scoped((" WHERE " + " AND ".join(clauses)) if clauses else "", params)


def active_filters(f):
    return {k: v for k, v in f.items() if k != "sort" and v not in ("", None)}


@bp.route("/")
@login_required
def search():
    db = get_db()
    f = read_filters(request.args)
    where, params = build_where(f)
    size = request.args.get("n", type=int)
    size = size if size in PAGE_SIZES else PAGE_SIZE
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = db.execute(f"SELECT COUNT(*) FROM voters{where}", params).fetchone()[0]
    pages = max(1, -(-total // size))
    page = min(page, pages)
    rows = db.execute(
        f"SELECT *, ({ASSIGNED_TO}) AS assigned_to, {WHO}{owner_columns()} FROM voters{where}"
        f" ORDER BY {SORTS[f['sort']]} LIMIT ? OFFSET ?",
        params + [size, (page - 1) * size],
    ).fetchall()
    w, p = scoped(" WHERE party != ''")
    parties = [r[0] for r in db.execute(f"SELECT DISTINCT party FROM voters{w} ORDER BY party", p)]
    w, p = scoped(" WHERE house_name != ''")
    houses = [r[0] for r in db.execute(
        f"SELECT DISTINCT house_name FROM voters{w} ORDER BY house_name COLLATE NOCASE", p)]
    w, p = scoped(" WHERE status != 'new'")
    contacted_by = db.execute(
        "SELECT id, username FROM users WHERE id IN"
        f" (SELECT DISTINCT status_by FROM voters{w}) ORDER BY username COLLATE NOCASE", p).fetchall()
    w, p = scoped()
    stats = db.execute(
        f"SELECT COUNT(*) AS total, SUM(sex = 'M') AS male, SUM(sex = 'F') AS female FROM voters{w}", p
    ).fetchone()
    query_args = {k: v for k, v in request.args.items() if k != "page"}
    # Counts per canvassing status for the chips, within every filter except the status itself
    where_all, params_all = build_where(dict(f, status=""))
    by_status = dict(db.execute(f"SELECT status, COUNT(*) FROM voters{where_all} GROUP BY status", params_all).fetchall())
    status_counts = {k: by_status.get(k, 0) for k in STATUSES}
    status_counts["all"] = sum(by_status.values())
    return render_template(
        "voters/list.html",
        statuses=STATUSES,
        status_counts=status_counts,
        back_url=request.full_path.rstrip("?"),
        chips=filter_chips(f, query_args),
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
        agents=agent_list() if is_admin() else [],
        contacted_by=contacted_by,
        agent_options=agent_list() if g.user["role"] in ("admin", "user") else [],
        size=size, sizes=PAGE_SIZES,
        first=(page - 1) * size + 1 if total else 0, last=min(page * size, total),
    )


def owner_columns():
    """Extra SELECT columns only the 'admin' account gets (whether a voter is 100% confirmed)."""
    from .confirmed import CONFIRMED_COLUMN

    return f", {CONFIRMED_COLUMN}" if is_owner() else ""


WHO = ("(SELECT username FROM users WHERE id = voters.status_by) AS status_by_name,"
       " (SELECT username FROM users WHERE id = voters.updated_by) AS updated_by_name")

ASSIGNED_TO = ("SELECT u.username FROM voter_assignments va JOIN users u ON u.id = va.user_id"
               " WHERE va.national_id = voters.national_id")


def agent_list():
    return get_db().execute(
        "SELECT id, username, is_active FROM users WHERE role = 'agent' ORDER BY is_active DESC, username COLLATE NOCASE"
    ).fetchall()


def _agent_name(value):
    if value == "any":
        return "Assigned one by one"
    row = get_db().execute("SELECT username FROM users WHERE id = ?", (int(value),)).fetchone()
    return f"Assigned to {row['username'] if row else '?'}"


def filter_chips(f, query_args):
    """(label, url without that filter) for each active filter, shown as removable chips."""
    labels = {
        "q": lambda v: f"“{v}”",
        "national_id": lambda v: f"ID {v}",
        "house": lambda v: f"House: {v}",
        "h": lambda v: f"🏠 {v}",
        "party": lambda v: "No party" if v == "(none)" else v,
        "sex": lambda v: {"M": "Male", "F": "Female", "unknown": "Sex not recorded"}[v],
        "age_min": lambda v: f"Age ≥ {v}",
        "age_max": lambda v: f"Age ≤ {v}",
        "va": _agent_name,
        "cf": lambda v: "100% confirmed" if v == "1" else "Not confirmed",
        "by": lambda v: _agent_name(v).replace("Assigned to", "Contacted by"),
        "ag": lambda v: "No agent" if v == "none" else _agent_name(v).replace("Assigned to", "Agent:"),
    }
    chips = []
    for key, value in active_filters(f).items():
        if key == "status":  # shown by the status chips instead
            continue
        rest = {k: v for k, v in query_args.items() if k != key}
        chips.append((labels[key](value), url_for("voters.search", **rest)))
    return chips


def get_voter_or_404(national_id):
    w, p = scoped(" WHERE national_id = ?", [national_id])
    row = get_db().execute(f"SELECT * FROM voters{w}", p).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/voters/<national_id>")
@login_required
def detail(national_id):
    voter = get_voter_or_404(national_id)
    db = get_db()
    editor = None
    if voter["updated_by"] and is_admin():
        editor = db.execute("SELECT username FROM users WHERE id = ?", (voter["updated_by"],)).fetchone()
    status_user = None
    if voter["status_by"]:
        status_user = db.execute("SELECT username FROM users WHERE id = ?", (voter["status_by"],)).fetchone()
    assigned = db.execute(
        "SELECT u.id, u.username FROM voter_assignments va JOIN users u ON u.id = va.user_id"
        " WHERE va.national_id = ?", (national_id,)).fetchone()
    house_agent = db.execute(
        "SELECT u.id, u.username FROM house_assignments a JOIN users u ON u.id = a.user_id WHERE a.house_name = ?",
        (voter["house_name"],)).fetchone() if voter["house_name"] else None
    confirmation = db.execute("SELECT * FROM confirmed_voters WHERE national_id = ?",
                              (national_id,)).fetchone() if is_owner() else None
    housemates = []
    if voter["house_name"]:
        w, p = scoped(" WHERE house_name = ? AND national_id != ?", [voter["house_name"], national_id])
        housemates = db.execute(
            f"SELECT *, {WHO}{owner_columns()} FROM voters{w} ORDER BY age IS NULL, age DESC, full_name COLLATE NOCASE LIMIT 60", p
        ).fetchall()
    return render_template("voters/detail.html", v=voter, editor=editor, statuses=STATUSES, housemates=housemates,
                           confirmation=confirmation,
                           status_user=status_user, back=_safe_back(request.args.get("back")),
                           assigned=assigned, house_agent=house_agent,
                           agents=agent_list() if is_admin() else [])


def _safe_back(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("voters.search")


@bp.post("/voters/<national_id>/canvass")
@login_required
def canvass(national_id):
    """Set the canvassing status and/or notes. Open to every signed-in user (canvassers)."""
    voter = get_voter_or_404(national_id)
    changes = {}
    status = request.form.get("status")
    if status is not None:
        if status not in STATUSES:
            abort(400)
        if status != voter["status"]:
            changes["status"] = {"from": voter["status"], "to": status}
    notes = request.form.get("notes")
    if notes is not None:
        notes = notes.replace("\r\n", "\n").strip()[:MAX_NOTES]
        if notes != voter["notes"]:
            changes["notes"] = {"from": voter["notes"], "to": notes}
    stamp = now_iso()
    if changes:
        db = get_db()
        db.execute(
            "UPDATE voters SET status = ?, notes = ?, status_at = ?, status_by = ? WHERE national_id = ?",
            (changes.get("status", {}).get("to", voter["status"]), changes.get("notes", {}).get("to", voter["notes"]),
             stamp, g.user["id"], national_id),
        )
        audit.log("voter_canvass", target=national_id, detail=changes, commit=False)
        db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "changed": bool(changes), "status": status or voter["status"],
                "label": STATUSES[status or voter["status"]], "by": g.user["username"],
                "at": current_app.jinja_env.filters["local"](stamp) if changes else None}
    flash("Saved." if changes else "Nothing changed.", "success" if changes else "info")
    back = _safe_back(request.form.get("back"))
    if request.form.get("from") == "list":
        back += f"#v-{national_id}"
    return redirect(back)


@bp.route("/photos/<national_id>")
@login_required
def photo(national_id):
    voter = get_voter_or_404(national_id)
    if not voter["photo_path"]:
        abort(404)
    resp = send_from_directory(current_app.config["PHOTOS_DIR"], voter["photo_path"], max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# --- admin: hand voters to agents one by one ---------------------------------

MAX_BULK_ASSIGN = 5000


def assign_voters(ids, agent_id):
    """Give these voters to an agent (None = back to their house's agent). Returns how many changed."""
    db = get_db()
    current = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        current.update(db.execute(
            f"SELECT national_id, user_id FROM voter_assignments WHERE national_id IN ({','.join('?' * len(chunk))})",
            chunk).fetchall())
    changed = [nid for nid in ids if current.get(nid) != agent_id]
    if not changed:
        return 0
    if agent_id is None:
        db.executemany("DELETE FROM voter_assignments WHERE national_id = ?", [(nid,) for nid in changed])
    else:
        stamp = now_iso()
        db.executemany(
            "INSERT INTO voter_assignments (national_id, user_id, assigned_at, assigned_by) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(national_id) DO UPDATE SET user_id = excluded.user_id,"
            " assigned_at = excluded.assigned_at, assigned_by = excluded.assigned_by",
            [(nid, agent_id, stamp, g.user["id"]) for nid in changed],
        )
    audit.log("voters_assigned", target=changed[0] if len(changed) == 1 else f"{len(changed)} voters",
              detail={"agent": agent_id, "voters": changed[:200]}, commit=False)
    db.commit()
    return len(changed)


def _chosen_agent(value):
    """'' means 'no individual agent'; anything else must be an agent's id (else 400)."""
    if value == "":
        return None, None
    row = get_db().execute("SELECT id, username FROM users WHERE id = ? AND role = 'agent'",
                           (int(value) if value.isdigit() else -1,)).fetchone()
    if row is None:
        abort(400)
    return row["id"], row["username"]


@bp.post("/voters/<national_id>/assign")
@admin_required
def assign(national_id):
    get_voter_or_404(national_id)
    agent_id, name = _chosen_agent(request.form.get("agent", ""))
    n = assign_voters([national_id], agent_id)
    if not n:
        flash("Nothing changed.", "info")
    else:
        flash(f"Assigned to {name}." if name else "Back with the house's agent.", "success")
    return redirect(url_for("voters.detail", national_id=national_id, back=_safe_back(request.form.get("back"))))


@bp.post("/voters/assign")
@admin_required
def assign_bulk():
    """Assign the ticked voters, or every voter matching the search filters, to one agent."""
    agent_id, name = _chosen_agent(request.form.get("agent", ""))
    back = _safe_back(request.form.get("back"))
    if request.form.get("scope") == "all":
        where, params = build_where(read_filters(request.form))
        ids = [r[0] for r in get_db().execute(
            f"SELECT national_id FROM voters{where} LIMIT ?", params + [MAX_BULK_ASSIGN + 1])]
        if len(ids) > MAX_BULK_ASSIGN:
            flash(f"That's more than {MAX_BULK_ASSIGN} voters. Narrow the filters first "
                  "(or assign whole houses on the Agents page).", "error")
            return redirect(back)
    else:
        wanted = list(dict.fromkeys(request.form.getlist("ids")))[:MAX_BULK_ASSIGN]
        ids = []
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            w, p = scoped(f" WHERE national_id IN ({','.join('?' * len(chunk))})", chunk)
            ids += [r[0] for r in get_db().execute(f"SELECT national_id FROM voters{w}", p)]
    if not ids:
        flash("Tick some voters first, or choose 'all matching voters'.", "error")
        return redirect(back)
    n = assign_voters(ids, agent_id)
    who = name or "their house's agent"
    flash(f"{n} voter{'s' if n != 1 else ''} now with {who}." if n else "Nothing changed.",
          "success" if n else "info")
    return redirect(back)


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
    db.execute("DELETE FROM voter_assignments WHERE national_id = ?", (national_id,))
    db.execute("DELETE FROM confirmed_voters WHERE national_id = ?", (national_id,))
    audit.log("voter_delete", target=national_id, detail={k: voter[k] for k in FIELDS}, commit=False)
    db.commit()
    flash(f"Voter {national_id} deleted.", "success")
    return redirect(url_for("voters.search"))


# --- admin: export ----------------------------------------------------------

EXPORT_HEADERS = ["ID", "Full name", "Sex", "Age", "House name", "Party", "Phone", "Has photo",
                  "Agent", "Agent via", "Status", "Status changed", "Status by", "Notes",
                  "Last edited", "Last edited by"]

# The agent responsible for a voter: the one they were handed to individually, else their house's agent
EXPORT_AGENT = (
    "COALESCE((SELECT u.username FROM voter_assignments va JOIN users u ON u.id = va.user_id"
    " WHERE va.national_id = voters.national_id),"
    " (SELECT u.username FROM house_assignments a JOIN users u ON u.id = a.user_id"
    " WHERE a.house_name = voters.house_name AND voters.house_name != '')) AS agent,"
    " EXISTS (SELECT 1 FROM voter_assignments va WHERE va.national_id = voters.national_id) AS one_by_one"
)


def _local(value):
    """Stored UTC timestamp -> Maldives time for spreadsheets ('2026-10-11 17:20')."""
    if not value:
        return ""
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (stamp + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")


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
    owner = is_owner()
    extra = (", (SELECT confirmed_at FROM confirmed_voters cv WHERE cv.national_id = voters.national_id)"
             " AS confirmed_at") if owner else ""
    rows = get_db().execute(
        f"SELECT *, {EXPORT_AGENT}, {WHO}{extra} FROM voters{where} ORDER BY {SORTS[f['sort']]}", params).fetchall()
    table = [
        [_safe_cell(r["national_id"]), _safe_cell(r["full_name"]), r["sex"], r["age"],
         _safe_cell(r["house_name"]), _safe_cell(r["party"]), r["phone"], "yes" if r["photo_path"] else "",
         _safe_cell(r["agent"] or ""), ("One by one" if r["one_by_one"] else "House") if r["agent"] else "",
         STATUSES.get(r["status"], r["status"]), _local(r["status_at"]),
         _safe_cell(r["status_by_name"] or ""), _safe_cell(r["notes"]),
         _local(r["updated_at"]) if r["updated_by"] else "",
         _safe_cell(r["updated_by_name"] or "")]
        + (["yes" if r["confirmed_at"] else "", _local(r["confirmed_at"])] if owner else [])
        for r in rows
    ]
    headers = EXPORT_HEADERS + (["100% confirmed", "Confirmed on"] if owner else [])
    # An export of the confirmed list is private to the 'admin' account, like the list itself
    audit.log("confirmed_export" if f.get("cf") else "export", target=fmt,
              detail={"filters": active_filters(f), "rows": len(rows)})
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    filename = f"voters-{stamp}.{fmt}"

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
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
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in table:
        ws.append(row)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col, width in zip("ABCDEFGHIJKLMNOPQR", (11, 32, 6, 6, 28, 14, 12, 10, 16, 12, 14, 17, 14, 40, 17, 14, 14, 17)):
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
