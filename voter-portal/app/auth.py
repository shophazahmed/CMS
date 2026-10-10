"""Login, sessions, CSRF and the RBAC decorators."""
import functools
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import audit
from .db import get_db, now_iso

bp = Blueprint("auth", __name__)

ROLES = ("admin", "user")
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 10

_hasher = PasswordHasher()  # Argon2id, library defaults
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password):
    return _hasher.hash(password)


def verify_password(stored_hash, password):
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password, username=""):
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters."
    if username and password.casefold() == username.casefold():
        return "The password can't be the same as the username."
    return None


def generate_password():
    return secrets.token_urlsafe(12)


# --- request hooks -----------------------------------------------------------

def load_user():
    """Attach the signed-in user to g, rejecting stale or disabled sessions."""
    g.user = None
    user_id = session.get("uid")
    if user_id is None:
        return
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    # A password change or deactivation ends every existing session.
    if (
        user is None
        or not user["is_active"]
        or session.get("pwd_at") != user["password_changed_at"]
    ):
        session.clear()
        return
    g.user = user


def csrf_token():
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_urlsafe(32)
    return token


def check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400, description="Your form expired. Reload the page and try again.")


# --- decorators --------------------------------------------------------------

def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if g.user["must_change_password"] and request.endpoint != "auth.change_password":
            flash("Choose a new password to continue.", "info")
            return redirect(url_for("auth.change_password"))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                audit.log("access_denied", target=request.path)
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


admin_required = role_required("admin")


def is_admin():
    return g.get("user") is not None and g.user["role"] == "admin"


# --- views -------------------------------------------------------------------

def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("voters.search")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("voters.search"))
    if request.method != "POST":
        return render_template("login.html", next=request.args.get("next", ""))

    db = get_db()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    now = datetime.now(timezone.utc)
    error = "Wrong username or password."

    if user is None:
        verify_password(_DUMMY_HASH, password)  # same timing as a real check
        audit.log("login_failed", target=username[:64], detail="unknown user")
    elif user["locked_until"] and datetime.fromisoformat(user["locked_until"]) > now:
        error = "Too many failed attempts. Try again in 15 minutes."
        audit.log("login_blocked", target=user["username"], detail="account locked")
    elif not verify_password(user["password_hash"], password):
        failed = user["failed_logins"] + 1
        locked = (now + LOCKOUT).replace(microsecond=0).isoformat() if failed >= MAX_FAILED_LOGINS else None
        db.execute(
            "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
            (0 if locked else failed, locked, user["id"]),
        )
        audit.log("login_failed", target=user["username"], detail="locked" if locked else "bad password")
    elif not user["is_active"]:
        error = "This account is deactivated. Ask an admin."
        audit.log("login_failed", target=user["username"], detail="inactive")
    else:
        if _hasher.check_needs_rehash(user["password_hash"]):
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
        db.execute(
            "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
            (now_iso(), user["id"]),
        )
        session.clear()
        session.permanent = True
        session["uid"] = user["id"]
        session["pwd_at"] = user["password_changed_at"]
        audit.log("login", user=user)
        return redirect(_safe_next(request.form.get("next")))

    flash(error, "error")
    return render_template("login.html", next=request.form.get("next", ""), username=username), 401


@bp.post("/logout")
def logout():
    if g.user is not None:
        audit.log("logout")
    session.clear()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        problem = None
        if not verify_password(g.user["password_hash"], current):
            problem = "Your current password is wrong."
        elif new != confirm:
            problem = "The new passwords don't match."
        elif new == current:
            problem = "Pick a password different from the current one."
        else:
            problem = password_problem(new, g.user["username"])
        if problem:
            flash(problem, "error")
        else:
            stamp = now_iso(precise=True)
            db = get_db()
            db.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0, password_changed_at = ?"
                " WHERE id = ?",
                (hash_password(new), stamp, g.user["id"]),
            )
            audit.log("password_changed", target=g.user["username"])
            session["pwd_at"] = stamp
            flash("Password changed.", "success")
            return redirect(url_for("voters.search"))
    return render_template("account/password.html")
