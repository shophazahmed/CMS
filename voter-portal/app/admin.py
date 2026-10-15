"""User management and the audit log (admins only)."""
import re

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import audit
from .auth import (CONFIRMED_ACTIONS, ROLE_LABELS, ROLES, admin_required, generate_password, hash_password, is_owner,
                   password_problem)
from .db import get_db, now_iso

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


@bp.route("/users")
@admin_required
def users():
    rows = get_db().execute("SELECT * FROM users ORDER BY is_active DESC, role, username COLLATE NOCASE").fetchall()
    return render_template("admin/users.html", users=rows, roles=ROLES, role_labels=ROLE_LABELS)


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
    elif role not in ROLES:
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

    if action == "toggle_active":
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
