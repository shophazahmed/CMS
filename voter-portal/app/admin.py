"""User management and the audit log (admins only)."""
import re
import secrets

from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from . import audit
from .auth import (CONFIRMED_ACCESS, CONFIRMED_ACTIONS, ROLE_LABELS, ROLES, admin_required, generate_password, hash_password, is_owner,
                   password_problem)
from .db import get_db, now_iso
from .normalize import clean_text, normalize_phone

bp = Blueprint("admin", __name__, url_prefix="/admin")

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
AUDIT_PAGE_SIZE = 100


def _get_user_or_404(user_id):
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        abort(404)
    return user


def _active_admins():
    return get_db().execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1").fetchone()[0]


WORK = ("(SELECT COUNT(*) FROM house_assignments a WHERE a.user_id = users.id) AS houses,"
        " (SELECT COUNT(*) FROM voter_assignments va WHERE va.user_id = users.id) AS singles")


@bp.route("/users")
@admin_required
def users():
    rows = get_db().execute(
        f"SELECT *, {WORK} FROM users WHERE deleted_at IS NULL"
        " ORDER BY is_active DESC, role, username COLLATE NOCASE").fetchall()
    agents = [u for u in rows if not u["can_login"]]
    logins = [u for u in rows if u["can_login"]]
    people = [u for u in rows if u["role"] in ("agent", "user", "admin")]
    owner_id = next((u["id"] for u in logins if is_owner(u)), None)
    from . import warroom

    boxes = [r[0] for r in get_db().execute("SELECT DISTINCT location FROM voters WHERE location != '' ORDER BY location")]
    everyone = sorted(rows, key=lambda u: (not u["is_active"], u["id"] != owner_id, u["username"].casefold()))
    return render_template("admin/users.html", agents=agents, users=logins, people=people, owner_id=owner_id,
                           everyone=everyone, cf_access=CONFIRMED_ACCESS, roles=("user", "admin"), role_labels=ROLE_LABELS,
                           stiers=warroom.stored_tiers, boxes=boxes, slug=_slug, view_names=warroom.VIEWS,
                           uviews=lambda u: [v for v in (u["war_views"] or "").split(",") if v in warroom.VIEWS] or list(warroom.VIEWS))


def _slug(name):
    """A sign-in username suggested from an agent's name ("Ilham Vatey" -> "ilham.vatey")."""
    import re

    out = re.sub(r"[^a-z0-9._-]+", ".", (name or "").casefold()).strip(".")[:32]
    return out if len(out) >= 3 else ""


@bp.route("/users/export.<fmt>")
@admin_required
def users_export(fmt):
    """Everyone on the Users page as CSV or Excel: one row a person. Never passwords. Access levels only for the owner."""
    import csv
    import io
    from datetime import datetime

    from flask import Response

    from . import warroom
    from .voters import _safe_cell

    if fmt not in ("csv", "xlsx"):
        abort(404)
    rows = get_db().execute(f"SELECT *, {WORK} FROM users WHERE deleted_at IS NULL"
                            " ORDER BY is_active DESC, can_login DESC, username COLLATE NOCASE").fetchall()
    owner = is_owner()
    level = lambda v: CONFIRMED_ACCESS.get(v or "", v or "")
    headers = ["#", "Name", "Sign-in", "Role", "Status", "Phone", "Note", "Last sign-in", "Houses", "One by one",
               "Election day", "Box (tier 1)"]
    if owner:
        headers += ["Sees", "Pledge sheet", "Super pledge & scenarios", "100% confirmed list"]
    table = []
    for n, u in enumerate(rows, 1):
        tiers = " + ".join(warroom.TIERS[t] for t in warroom.stored_tiers(u))
        line = [n, _safe_cell(u["username"]), "yes" if u["can_login"] else "no (agent)",
                "owner" if is_owner(u) else u["role"], "active" if u["is_active"] else "deactivated",
                u["phone"] or "", _safe_cell(u["note"] or ""),
                (u["last_login_at"] or "")[:16].replace("T", " "), u["houses"], u["singles"], tiers,
                _safe_cell(u["war_box"] or "") if "t1" in warroom.stored_tiers(u) else ""]
        if owner:
            line += ["owner" if is_owner(u) else ("pledge sheet only" if u["pledge_only"] else "whole portal"),
                     level(u["pledge_access"]), level(u["super_access"]), level(u["confirmed_access"])]
        table.append(line)
    audit.log("users_export", target=fmt, detail={"rows": len(table)})
    name = f"users-{datetime.now().strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(table)
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Users"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for line in table:
        ws.append(line)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), headers={"Content-Disposition": f'attachment; filename="{name}"'},
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _clean_name(value):
    return clean_text(value)[:60]


@bp.post("/agents")
@admin_required
def create_agent():
    """A canvasser listed by name and phone. No password: they never sign in."""
    db = get_db()
    name = _clean_name(request.form.get("name"))
    phone, _ = normalize_phone(request.form.get("phone", ""))
    note = clean_text(request.form.get("note", ""))[:200]
    if len(name) < 2:
        flash("Give the agent a name.", "error")
    elif db.execute("SELECT 1 FROM users WHERE username = ?", (name,)).fetchone():
        flash(f"'{name}' is already in the list. Add something to tell them apart (e.g. their house).", "error")
    else:
        stamp = now_iso(precise=True)
        db.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password, password_changed_at,"
            " created_at, phone, note, can_login) VALUES (?, ?, 'agent', 1, 0, ?, ?, ?, ?, 0)",
            (name, hash_password(secrets.token_urlsafe(24)), stamp, stamp, phone, note),
        )
        audit.log("agent_create", target=name, detail={"phone": phone} if phone else None, commit=False)
        db.commit()
        flash(f"Agent {name} added. Give them houses on the Agents page, or voters from Search.", "success")
    return redirect(url_for("admin.users"))


def _move_work(src, dst):
    """Hand a person's houses and one-by-one voters to someone else (dst None: back to unassigned)."""
    db = get_db()
    if dst is None:
        houses = db.execute("DELETE FROM house_assignments WHERE user_id = ?", (src,)).rowcount
        singles = db.execute("DELETE FROM voter_assignments WHERE user_id = ?", (src,)).rowcount
    else:
        stamp, by = now_iso(), g.user["id"]
        houses = db.execute("UPDATE house_assignments SET user_id = ?, assigned_at = ?, assigned_by = ?"
                            " WHERE user_id = ?", (dst, stamp, by, src)).rowcount
        singles = db.execute("UPDATE voter_assignments SET user_id = ?, assigned_at = ?, assigned_by = ?"
                             " WHERE user_id = ?", (dst, stamp, by, src)).rowcount
    return houses, singles


def _target_person(value, not_id):
    """'' = unassigned; otherwise an active agent or user (not the one being emptied)."""
    if not value:
        return None, "unassigned"
    row = get_db().execute(
        "SELECT id, username FROM users WHERE id = ? AND id != ? AND role IN ('agent', 'user', 'admin')"
        " AND deleted_at IS NULL AND is_active = 1", (int(value) if value.isdigit() else -1, not_id)).fetchone()
    if row is None:
        abort(400)
    return row["id"], row["username"]


@bp.post("/users")
@admin_required
def create_user():
    db = get_db()
    username = request.form.get("username", "").strip()
    role = request.form.get("role", "user")
    password = request.form.get("password", "")
    generated = not password
    if generated:
        password = generate_password()
    must_change = 1 if request.form.get("must_change") == "1" else 0
    problem = None
    if not USERNAME_RE.match(username):
        problem = "Username: 3-32 letters, digits, dot, dash or underscore."
    elif role not in ("user", "admin") and not (role == "pledge" and is_owner()):
        problem = "Pick a valid role."
    elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        problem = f"Username '{username}' is taken."
    else:
        problem = password_problem(password, username)
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.users"))
    stamp = now_iso(precise=True)
    pledge_only = role == "pledge"  # a read-only account that sees only the pledge sheet (view and mark)
    db.execute(
        "INSERT INTO users (username, password_hash, role, is_active, must_change_password,"
        " password_changed_at, created_at, pledge_only, pledge_access) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
        (username, hash_password(password), "user" if pledge_only else role, must_change, stamp, stamp,
         1 if pledge_only else 0, "mark" if pledge_only else ""),
    )
    audit.log("user_create", target=username, detail={"role": role}, commit=False)
    db.commit()
    then = " They choose their own at first sign-in." if must_change else ""
    if generated:
        flash(f"User {username} created.{then} Password (shown once): {password}", "secret")
    else:
        flash(f"User {username} created with the password you set.{then}", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>")
@admin_required
def update_user(user_id):
    db = get_db()
    user = _get_user_or_404(user_id)
    action = request.form.get("action")
    is_self = user["id"] == g.user["id"]
    last_admin = user["role"] == "admin" and user["is_active"] and _active_admins() <= 1
    # The owner account holds the confirmed-voter list: other admins can't take it over
    if is_owner(user) and not is_self and action in ("toggle_active", "set_role", "reset_password", "set_password", "rename"):
        audit.log("access_denied", target=f"user {user['username']}", detail={"action": action})
        flash(f"Only {user['username']} can change the {user['username']} account.", "error")
        return redirect(url_for("admin.users"))

    if user["deleted_at"]:
        abort(404)

    if action == "set_pledge_only":
        if not is_owner() or is_owner(user) or user["role"] == "admin" or not user["can_login"]:
            audit.log("access_denied", target=f"user {user['username']}", detail={"action": action})
            abort(403)
        on = request.form.get("on") == "1"
        if on != bool(user["pledge_only"]):
            # someone who may see only the pledge sheet needs at least View on it
            access = user["pledge_access"] or ("view" if on else "")
            db.execute("UPDATE users SET pledge_only = ?, pledge_access = ? WHERE id = ?", (1 if on else 0, access, user_id))
            audit.log("pledge_only", target=user["username"], detail={"on": on}, commit=False)
            flash(f"{user['username']} now sees {'only the pledge sheet and distribution' if on else 'the whole portal again'}.",
                  "success")

    elif action == "set_pledge_access":
        if not is_owner() or is_owner(user) or not user["can_login"]:
            audit.log("access_denied", target=f"user {user['username']}", detail={"action": action})
            abort(403)
        level = request.form.get("level", "")
        if level not in CONFIRMED_ACCESS:
            abort(400)
        if level != user["pledge_access"]:
            db.execute("UPDATE users SET pledge_access = ? WHERE id = ?", (level, user_id))
            audit.log("pledge_access", target=user["username"], detail={"from": user["pledge_access"], "to": level})
            flash(f"{user['username']}: pledge sheet {CONFIRMED_ACCESS[level].lower()}.", "success")

    elif action == "set_super_access":
        if not is_owner() or is_owner(user) or not user["can_login"]:
            audit.log("access_denied", target=f"user {user['username']}", detail={"action": action})
            abort(403)
        level = request.form.get("level", "")
        if level not in CONFIRMED_ACCESS:
            abort(400)
        if level != user["super_access"]:
            db.execute("UPDATE users SET super_access = ? WHERE id = ?", (level, user_id))
            audit.log("super_access", target=user["username"], detail={"from": user["super_access"], "to": level})
            flash(f"{user['username']}: super pledge & scenarios {CONFIRMED_ACCESS[level].lower()}.", "success")

    elif action == "set_confirmed_access":
        # Only the owner hands out the 100% confirmed list, and only to people who sign in
        if not is_owner() or is_owner(user) or not user["can_login"]:
            audit.log("access_denied", target=f"user {user['username']}", detail={"action": action})
            abort(403)
        level = request.form.get("level", "")
        if level not in CONFIRMED_ACCESS:
            abort(400)
        if level != user["confirmed_access"]:
            db.execute("UPDATE users SET confirmed_access = ? WHERE id = ?", (level, user_id))
            audit.log("confirmed_access", target=user["username"], detail={"from": user["confirmed_access"], "to": level})
            flash(f"{user['username']}: confirmed list {CONFIRMED_ACCESS[level].lower()}.", "success")

    elif action == "rename":
        new = request.form.get("username", "").strip()
        if not user["can_login"]:
            abort(400)  # agents are renamed with Edit
        if not USERNAME_RE.match(new):
            flash("Username: 3-32 letters, digits, dot, dash or underscore.", "error")
        elif new.casefold() == user["username"].casefold() and new == user["username"]:
            flash("That's already the username.", "info")
        elif db.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (new, user_id)).fetchone():
            flash(f"Username '{new}' is taken.", "error")
        else:
            owner = is_owner(user)  # decided before the rename: the flag keeps it afterwards
            db.execute("UPDATE users SET username = ?, is_owner = ? WHERE id = ?",
                       (new, 1 if owner else user["is_owner"], user_id))
            audit.log("user_rename", target=new, detail={"from": user["username"]})
            flash(f"{user['username']} is now {new}." + (" Sign in with the new username from now on." if is_self else ""),
                  "success")

    elif action == "edit_agent":
        name = _clean_name(request.form.get("name"))
        phone, _ = normalize_phone(request.form.get("phone", ""))
        note = clean_text(request.form.get("note", ""))[:200]
        if user["can_login"]:
            abort(400)  # sign-in accounts keep their username
        if len(name) < 2:
            flash("Give the agent a name.", "error")
        elif name != user["username"] and db.execute(
                "SELECT 1 FROM users WHERE username = ? AND id != ?", (name, user_id)).fetchone():
            flash(f"'{name}' is already in the list.", "error")
        else:
            db.execute("UPDATE users SET username = ?, phone = ?, note = ? WHERE id = ?", (name, phone, note, user_id))
            audit.log("agent_update", target=name, detail={"from": user["username"]} if name != user["username"] else None)
            flash(f"Saved {name}.", "success")

    elif action == "move_work":
        dst, dst_name = _target_person(request.form.get("to", ""), user_id)
        houses, singles = _move_work(user_id, dst)
        audit.log("work_moved", target=user["username"], detail={"to": dst_name, "houses": houses, "voters": singles})
        flash(f"Moved {houses} house{'s' if houses != 1 else ''} and {singles} one-by-one voter"
              f"{'s' if singles != 1 else ''} from {user['username']} to {dst_name}." if houses or singles
              else f"{user['username']} had no houses or voters.", "success" if houses or singles else "info")

    elif action == "delete":
        if is_self or last_admin or is_owner(user):
            flash("You can't delete yourself, the last active admin or the admin account.", "error")
        elif request.form.get("confirm", "").strip().casefold() != user["username"].casefold():
            flash(f"Type the name {user['username']} to confirm the delete.", "error")
        else:
            dst, dst_name = _target_person(request.form.get("to", ""), user_id)
            houses, singles = _move_work(user_id, dst)
            gone = f"{user['username']} (deleted)"
            if db.execute("SELECT 1 FROM users WHERE username = ?", (gone,)).fetchone():
                gone = f"{user['username']} (deleted {user_id})"
            # Kept as a row so past canvassing still shows who did it; frees the name for reuse
            db.execute("UPDATE users SET username = ?, is_active = 0, can_login = 0, deleted_at = ? WHERE id = ?",
                       (gone, now_iso(), user_id))
            audit.log("user_delete", target=user["username"],
                      detail={"houses": houses, "voters": singles, "work_to": dst_name})
            flash(f"{user['username']} deleted. {houses} house{'s' if houses != 1 else ''} and {singles} voter"
                  f"{'s' if singles != 1 else ''} moved to {dst_name}.", "success")

    elif action == "toggle_login":
        if is_self or is_owner(user) or user["role"] == "admin":
            flash("Admins always sign in.", "error")
        elif user["can_login"]:
            db.execute("UPDATE users SET can_login = 0 WHERE id = ?", (user_id,))
            audit.log("user_login_off", target=user["username"])
            flash(f"{user['username']} can no longer sign in; their houses and voters stay with them.", "success")
        else:
            # the same person keeps their houses, voters, pledges and history: no new account
            new = (request.form.get("username") or user["username"]).strip()
            if not USERNAME_RE.match(new):
                flash("To sign in, pick a username: 3-32 letters, digits, dot, dash or underscore (no spaces).", "error")
            elif db.execute("SELECT 1 FROM users WHERE username = ? AND id != ?", (new, user_id)).fetchone():
                flash(f"Username '{new}' is taken.", "error")
            elif request.form.get("password") and password_problem(request.form["password"], new):
                flash(password_problem(request.form["password"], new), "error")
            else:
                chosen = request.form.get("password") or ""
                password = chosen or generate_password()
                must_change = 1 if request.form.get("must_change") == "1" else 0
                db.execute(
                    "UPDATE users SET can_login = 1, username = ?, role = CASE role WHEN 'agent' THEN 'user' ELSE role END,"
                    " password_hash = ?, must_change_password = ?, password_changed_at = ?,"
                    " failed_logins = 0, locked_until = NULL WHERE id = ?",
                    (new, hash_password(password), must_change, now_iso(precise=True), user_id))
                audit.log("user_login_on", target=new, detail={"from": user["username"]} if new != user["username"] else None)
                if chosen:
                    flash(f"{user['username']} can now sign in as {new} with the password you set.", "success")
                else:
                    flash(f"{user['username']} can now sign in as {new}. Password (shown once): {password}", "secret")

    elif action == "toggle_active":
        if is_self or last_admin:
            flash("You can't deactivate yourself or the last active admin.", "error")
        else:
            new = 0 if user["is_active"] else 1
            db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new, user_id))
            audit.log("user_activate" if new else "user_deactivate", target=user["username"])
            flash(f"{user['username']} {'activated' if new else 'deactivated'}.", "success")

    elif action == "set_role":
        role = request.form.get("role")
        if role not in ROLES:
            abort(400)
        if role != user["role"] and (is_self or last_admin):
            flash("You can't change your own role or demote the last active admin.", "error")
        elif role != user["role"]:
            db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
            audit.log("user_role", target=user["username"], detail={"from": user["role"], "to": role})
            flash(f"{user['username']} is now {role}.", "success")

    elif action == "set_password":
        # the admin chooses the password and gives it to the person; they keep it unless asked to change it
        password = request.form.get("password", "")
        problem = password_problem(password, user["username"])
        if is_self:
            flash("Change your own password from 'Change password' instead.", "error")
        elif not user["can_login"]:
            flash("Allow sign-in first.", "error")
        elif problem:
            flash(problem, "error")
        else:
            must_change = 1 if request.form.get("must_change") == "1" else 0
            db.execute(
                "UPDATE users SET password_hash = ?, must_change_password = ?, password_changed_at = ?,"
                " failed_logins = 0, locked_until = NULL WHERE id = ?",
                (hash_password(password), must_change, now_iso(precise=True), user_id))
            audit.log("user_password_set", target=user["username"], detail={"must_change": bool(must_change)})
            flash(f"Password set for {user['username']}." + (" They choose their own at first sign-in." if must_change else "")
                  + " Their other sessions end.", "success")

    elif action == "reset_password":
        if is_self:
            flash("Change your own password from 'Change password' instead.", "error")
        else:
            password = generate_password()
            db.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 1, password_changed_at = ?,"
                " failed_logins = 0, locked_until = NULL WHERE id = ?",
                (hash_password(password), now_iso(precise=True), user_id),
            )
            audit.log("user_password_reset", target=user["username"])
            flash(f"New temporary password for {user['username']} (shown once): {password}", "secret")

    elif action == "unlock":
        db.execute("UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = ?", (user_id,))
        audit.log("user_unlock", target=user["username"])
        flash(f"{user['username']} unlocked.", "success")
    else:
        abort(400)
    db.commit()
    return redirect(url_for("admin.users"))


@bp.route("/audit")
@admin_required
def audit_log():
    db = get_db()
    action = request.args.get("action", "")
    username = request.args.get("user", "").strip()
    target = request.args.get("target", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    clauses, params = [], []
    if action:
        clauses.append("action = ?")
        params.append(action)
    if username:
        clauses.append("username = ? COLLATE NOCASE")
        params.append(username)
    if target:
        clauses.append("target = ? COLLATE NOCASE")
        params.append(target)
    if not is_owner():  # confirmed-voter changes are private to the 'admin' account
        clauses.append(f"action NOT IN ({','.join('?' * len(CONFIRMED_ACTIONS))})")
        params += list(CONFIRMED_ACTIONS)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = db.execute(f"SELECT COUNT(*) FROM audit_log{where}", params).fetchone()[0]
    pages = max(1, -(-total // AUDIT_PAGE_SIZE))
    rows = db.execute(
        f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [AUDIT_PAGE_SIZE, (min(page, pages) - 1) * AUDIT_PAGE_SIZE],
    ).fetchall()
    actions = [r[0] for r in db.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
               if is_owner() or r[0] not in CONFIRMED_ACTIONS]
    query_args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template(
        "admin/audit.html", rows=rows, actions=actions, action=action, username=username,
        target=target, page=min(page, pages), pages=pages, total=total, query_args=query_args,
    )


# --- changes: who changed what, in plain words ----------------------------------------

CHANGE_TYPES = {
    "voter_canvass": "Status / note",
    "voter_note_delete": "Note deleted",
    "voter_update": "Voter edited",
    "voter_create": "Voter added",
    "voter_delete": "Voter deleted",
    "voters_assigned": "Voters assigned",
    "houses_assigned": "Houses assigned",
    "houses_auto_assigned": "Houses auto-assigned",
    "work_moved": "Work moved",
    "unassign_all": "Everyone unassigned",
    "import_run": "Import",
    "voter_share": "Shared on WhatsApp",
    "voter_confirm": "Confirmed (100%)",
    "voter_unconfirm": "Unconfirmed",
}
CHANGES_PAGE = 100


def _describe(row, statuses, names):
    """One line saying what changed, from the stored detail."""
    import json

    try:
        d = json.loads(row["detail"]) if row["detail"] and row["detail"][:1] in "{[" else row["detail"]
    except ValueError:
        d = row["detail"]
    a = row["action"]
    if a == "voter_canvass" and isinstance(d, dict):
        parts = []
        if "status" in d:
            parts.append(f"Status: {statuses.get(d['status']['from'], d['status']['from'])} → "
                         f"{statuses.get(d['status']['to'], d['status']['to'])}")
        if d.get("note"):
            parts.append(f"Note: {d['note']}")
        if "notes" in d:  # notes before they became dated entries
            parts.append(f"Notes: {d['notes'].get('to') or '(cleared)'}")
        return " · ".join(parts)
    if a == "voter_update" and isinstance(d, dict):
        return " · ".join(f"{k.replace('_', ' ')}: {v.get('from') or '—'} → {v.get('to') or '—'}"
                          for k, v in d.items() if isinstance(v, dict))
    if a == "voter_note_delete" and isinstance(d, dict):
        return f"Note: {d.get('note', '')}"
    if a == "voters_assigned" and isinstance(d, dict):
        who = names.get(d.get("agent"), "their house's person") if d.get("agent") else "their house's person"
        return f"{row['target']} → {who}"
    if a == "voter_share" and isinstance(d, dict):
        return (f"{len(d.get('voters', []))} voter card(s) to {d.get('to')} · "
                f"{'text' if d.get('mode') == 'text' else 'card'} · {'Dhivehi' if d.get('lang') == 'dv' else 'English'}"
                + (f" · {d['assigned']} assigned" if d.get("assigned") else ""))
    if isinstance(d, dict):
        return ", ".join(f"{k}: {v}" for k, v in d.items() if not isinstance(v, (list, dict)))
    return d or ""


def _changes_query(args):
    clauses = [f"a.action IN ({','.join('?' * len(CHANGE_TYPES))})"]
    params = list(CHANGE_TYPES)
    if not is_owner():
        clauses.append(f"a.action NOT IN ({','.join('?' * len(CONFIRMED_ACTIONS))})")
        params += list(CONFIRMED_ACTIONS)
    person = args.get("person", "")
    if person.isdigit():
        clauses.append("a.user_id = ?")
        params.append(int(person))
    kind = args.get("type", "")
    if kind in CHANGE_TYPES:
        clauses.append("a.action = ?")
        params.append(kind)
    for key, op in (("from", ">="), ("to", "<")):
        day = args.get(key, "")
        try:
            start = datetime.strptime(day, "%Y-%m-%d") - timedelta(hours=5)  # Maldives day -> UTC
        except ValueError:
            continue
        if key == "to":
            start += timedelta(days=1)
        clauses.append(f"a.at {op} ?")
        params.append(start.replace(tzinfo=timezone.utc).isoformat(timespec="seconds"))
    q = args.get("q", "").strip()
    if q:
        clauses.append("(a.target LIKE ? OR v.full_name LIKE ? OR v.house_name LIKE ?)")
        params += [f"%{q}%"] * 3
    sql = (" FROM audit_log a LEFT JOIN voters v ON v.national_id = a.target"
           " WHERE " + " AND ".join(clauses))
    return sql, params


@bp.route("/changes")
@admin_required
def changes():
    """Every change people made (status, notes, edits, assignments), with who and when."""
    from .voters import STATUSES

    db = get_db()
    sql, params = _changes_query(request.args)
    names = dict(db.execute("SELECT id, username FROM users").fetchall())
    if request.args.get("format") == "csv":
        import csv
        import io

        rows = db.execute(f"SELECT a.*, v.full_name, v.house_name{sql} ORDER BY a.id DESC LIMIT 20000", params).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["When (Maldives)", "Person", "Type", "ID", "Voter", "House", "What changed"])
        for r in rows:
            when = (datetime.fromisoformat(r["at"]) + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
            w.writerow([when, r["username"] or "", CHANGE_TYPES.get(r["action"], r["action"]), r["target"] or "",
                        r["full_name"] or "", r["house_name"] or "", _describe(r, STATUSES, names)])
        audit.log("export", target="changes.csv", detail={"rows": len(rows)})
        return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="changes-{datetime.now():%Y%m%d}.csv"'})
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = db.execute(f"SELECT COUNT(*){sql}", params).fetchone()[0]
    pages = max(1, -(-total // CHANGES_PAGE))
    page = min(page, pages)
    rows = db.execute(f"SELECT a.*, v.full_name, v.house_name{sql} ORDER BY a.id DESC LIMIT ? OFFSET ?",
                      params + [CHANGES_PAGE, (page - 1) * CHANGES_PAGE]).fetchall()
    people = db.execute("SELECT DISTINCT a.user_id AS id, a.username FROM audit_log a WHERE a.user_id IS NOT NULL"
                        f" AND a.action IN ({','.join('?' * len(CHANGE_TYPES))}) ORDER BY a.username COLLATE NOCASE",
                        list(CHANGE_TYPES)).fetchall()
    types = {k: v for k, v in CHANGE_TYPES.items() if is_owner() or k not in CONFIRMED_ACTIONS}
    query_args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("admin/changes.html", rows=rows, total=total, page=page, pages=pages, people=people,
                           types=types, args=request.args, query_args=query_args,
                           describe=lambda r: _describe(r, STATUSES, names))
