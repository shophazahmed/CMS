"""User management and the audit log (admins only)."""
import re
import secrets

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import audit
from .auth import (CONFIRMED_ACTIONS, ROLE_LABELS, ROLES, admin_required, generate_password, hash_password, is_owner,
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
    return render_template("admin/users.html", agents=agents, users=logins, people=people,
                           roles=("user", "admin"), role_labels=ROLE_LABELS)


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
    problem = None
    if not USERNAME_RE.match(username):
        problem = "Username: 3-32 letters, digits, dot, dash or underscore."
    elif role not in ("user", "admin"):
        problem = "Pick a valid role."
    elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        problem = f"Username '{username}' is taken."
    else:
        problem = password_problem(password, username)
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.users"))
    stamp = now_iso(precise=True)
    db.execute(
        "INSERT INTO users (username, password_hash, role, is_active, must_change_password,"
        " password_changed_at, created_at) VALUES (?, ?, ?, 1, 1, ?, ?)",
        (username, hash_password(password), role, stamp, stamp),
    )
    audit.log("user_create", target=username, detail={"role": role}, commit=False)
    db.commit()
    if generated:
        flash(f"User {username} created. Temporary password (shown once): {password}", "secret")
    else:
        flash(f"User {username} created. They must change the password at first sign-in.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>")
@admin_required
def update_user(user_id):
    db = get_db()
    user = _get_user_or_404(user_id)
    action = request.form.get("action")
    is_self = user["id"] == g.user["id"]
    last_admin = user["role"] == "admin" and user["is_active"] and _active_admins() <= 1
    # The 'admin' account holds the confirmed-voter list: other admins can't take it over
    if is_owner(user) and not is_self and action in ("toggle_active", "set_role", "reset_password"):
        audit.log("access_denied", target=f"user {user['username']}", detail={"action": action})
        flash(f"Only {user['username']} can change the {user['username']} account.", "error")
        return redirect(url_for("admin.users"))

    if user["deleted_at"]:
        abort(404)

    if action == "edit_agent":
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
        elif not USERNAME_RE.match(user["username"]):
            flash("To sign in, the name must be a username (3-32 letters or digits, no spaces). Rename the agent first.",
                  "error")
        else:
            password = generate_password()
            db.execute(
                "UPDATE users SET can_login = 1, password_hash = ?, must_change_password = 1, password_changed_at = ?,"
                " failed_logins = 0, locked_until = NULL WHERE id = ?",
                (hash_password(password), now_iso(precise=True), user_id))
            audit.log("user_login_on", target=user["username"])
            flash(f"{user['username']} can now sign in. Temporary password (shown once): {password}", "secret")

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
