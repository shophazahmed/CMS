"""Other races counted on the same night (e.g. GDh - Atoll GM Member), apart from the island council count.

Each race has its own candidates (ballot number, full name, island and house, photo), seats and votes per
ballot, and is counted box by box at the same ballot boxes: tap the candidates marked on each ballot, then
Count ballot; enter the official sheet when the box is done. Results add up every box.

Who: the owner and War Room accounts with the Vote count view see every race. Accounts given an atoll count
role (Users page) see ONLY that race: a box counter taps ballots at their box; a results viewer sees every box
and the totals and can enter the official sheets. Those accounts reach nothing else in the portal.
"""
import io
import json
import os
import secrets
from datetime import datetime, timezone

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from . import audit
from .auth import is_admin, is_owner, login_required
from .db import get_db, now_iso
from .normalize import clean_text

bp = Blueprint("racecount", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    seats      INTEGER NOT NULL DEFAULT 2,
    max_votes  INTEGER NOT NULL DEFAULT 2,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS race_candidates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id    INTEGER NOT NULL,
    ballot_no  INTEGER,
    name       TEXT NOT NULL,
    place      TEXT NOT NULL DEFAULT '',
    photo_path TEXT,
    position   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS race_tally (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id    INTEGER NOT NULL,
    box        TEXT NOT NULL,
    combo      TEXT NOT NULL,          -- p:<a>-<b> (a pair), s:<a> (one vote), x (invalid / blank), undo
    at         TEXT NOT NULL,
    by_user    INTEGER,
    client_key TEXT,
    voided_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_race_tally_key ON race_tally (client_key);
CREATE INDEX IF NOT EXISTS ix_race_tally_box ON race_tally (race_id, box, voided_at);
CREATE TABLE IF NOT EXISTS race_box (
    race_id    INTEGER NOT NULL,
    box        TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'counting' CHECK (status IN ('counting', 'final')),
    official   TEXT NOT NULL DEFAULT '{}',
    photo      TEXT,
    final_at   TEXT,
    final_by   INTEGER,
    note       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (race_id, box)
);
"""
ROLES = {"": "No access", "count": "Box counter", "watch": "Results viewer", "admin": "Atoll administrator"}
MAX_PHOTO = 12 * 1024 * 1024
# What an atoll-count-only account may open
RACE_ENDPOINTS = {
    "racecount.home", "racecount.sheet", "racecount.tap", "racecount.final", "racecount.reopen", "racecount.sheet_photo",
    "racecount.results", "racecount.results_xlsx", "racecount.candidate_photo", "racecount.manage",
    "theme.theme_css", "theme.set_appearance", "fonts.stylesheet", "fonts.serve", "auth.login", "auth.logout",
    "auth.change_password", "static",
}


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _dir():
    path = os.path.join(current_app.config["CAMPAIGN_DIR"], "races")
    os.makedirs(path, exist_ok=True)
    return path


# --- access ---------------------------------------------------------------------------------------

def race_role(user=None):
    """'count' / 'watch' for an atoll count account (signed in, active), else ''."""
    user = user if user is not None else g.get("user")
    if user is None or user["role"] == "admin" or not user["can_login"] or user["deleted_at"] or not user["is_active"]:
        return ""
    return user["race_role"] if user["race_role"] in ("count", "watch", "admin") and user["race_id"] else ""


def is_race_only(user=None):
    return bool(race_role(user))


def race_guard():
    """Before every request: atoll count accounts reach only their race's pages."""
    if not is_race_only() or request.endpoint in RACE_ENDPOINTS:
        return None
    if request.method in ("GET", "HEAD"):
        return redirect(url_for("racecount.home"))
    audit.log("access_denied", target=request.path, detail={"race_only": True})
    abort(403)


def can_watch(race_id):
    """Sees every box and the results of this race."""
    from .warroom import has_view

    user = g.get("user")
    if user is None:
        return False
    if is_race_only():
        return race_role() in ("watch", "admin") and user["race_id"] == race_id
    return is_owner() or has_view("count")


def t1_box():
    """A tier 1 box agent given the atoll count: their box (else '')."""
    from .warroom import tiers

    user = g.get("user")
    if user is None or is_race_only() or "t1" not in tiers() or not user["race_t1"]:
        return ""
    return user["war_box"] or ""


def my_box(race_id):
    """The box this request counts: a box counter's or tier 1 box agent's own box, or ?box= for those who watch."""
    from .warroom import is_open

    user = g.get("user")
    if is_race_only() and race_role() == "count":
        if user["race_id"] != race_id:
            abort(403)
        return user["race_box"] or ""
    if can_watch(race_id):
        return (request.values.get("box") or "")[:200]
    if t1_box():
        if not is_open():
            abort(423)  # like their island count: only while the War Room is open
        race = get_db().execute("SELECT active FROM races WHERE id = ?", (race_id,)).fetchone()
        if not race or not race["active"]:
            abort(403)
        return t1_box()
    abort(403)


def may_count(race_id):
    """Counts (or watches) this race in some way: for photos and the menu."""
    return can_watch(race_id) or (is_race_only() and g.user["race_id"] == race_id) or bool(t1_box())


def can_manage(race_id=None):
    """Edits races and candidates: the owner and admins (every race, and adding races); an atoll administrator
    their own race only (race_id None: may open the page)."""
    if is_owner() or is_admin():
        return True
    if race_role() == "admin":
        return race_id is None or g.user["race_id"] == race_id
    return False


def is_portal_admin():
    return is_owner() or is_admin()


def nav_races():
    """The races this account sees in the menu."""
    if g.get("user") is None:
        return []
    try:
        rows = get_db().execute("SELECT id, name FROM races WHERE active = 1 ORDER BY id").fetchall()
    except Exception:
        return []
    if is_race_only():
        return [r for r in rows if r["id"] == g.user["race_id"]]
    return [r for r in rows if can_watch(r["id"]) or t1_box()]


# --- data -----------------------------------------------------------------------------------------

def race_or_404(rid):
    row = get_db().execute("SELECT * FROM races WHERE id = ?", (rid,)).fetchone()
    if row is None:
        abort(404)
    return row


def candidates(rid):
    return get_db().execute("SELECT * FROM race_candidates WHERE race_id = ? ORDER BY ballot_no IS NULL, ballot_no,"
                            " position, id", (rid,)).fetchall()


def label(c):
    return f"#{c['ballot_no']} {c['name']}" if c["ballot_no"] else c["name"]


def combos(cands, max_votes):
    out = []
    if max_votes != 1:
        for i, a in enumerate(cands):
            for b in cands[i + 1:]:
                out.append((f"p:{a['id']}-{b['id']}", f"{label(a)} + {label(b)}", (a["id"], b["id"])))
    for a in cands:
        out.append((f"s:{a['id']}", f"{label(a)} only", (a["id"],)))
    out.append(("x", "Invalid / blank", ()))
    return out


def boxes():
    """The same ballot boxes as the island count: each official list's box, else the portal's box."""
    from .warroom import OFF_LIST, box_expr

    rows = get_db().execute(f"SELECT DISTINCT {box_expr('voters')} AS b FROM voters WHERE deceased = 0").fetchall()
    return sorted({r[0] for r in rows if r[0] and r[0] != OFF_LIST}, key=str.casefold)


def tally(rid, box=None):
    sql, params = "SELECT box, combo, COUNT(*) FROM race_tally WHERE race_id = ? AND voided_at IS NULL", [rid]
    if box is not None:
        sql, params = sql + " AND box = ?", params + [box]
    out = {}
    for b, combo, n in get_db().execute(sql + " GROUP BY box, combo", params):
        out.setdefault(b, {})[combo] = n
    return out


def totals_from(tal):
    votes, invalid, ballots = {}, 0, 0
    for combo, n in tal.items():
        ballots += n
        if combo == "x":
            invalid += n
            continue
        for cid in combo[2:].split("-"):
            if cid.isdigit():
                votes[int(cid)] = votes.get(int(cid), 0) + n
    return votes, invalid, ballots


def box_state(rid, box):
    row = get_db().execute("SELECT * FROM race_box WHERE race_id = ? AND box = ?", (rid, box)).fetchone()
    return row, (json.loads(row["official"]) if row and row["official"] else {})


def results(rid):
    race = race_or_404(rid)
    cands = candidates(rid)
    ids = [c["id"] for c in cands]
    tal_all = tally(rid)
    states = {r["box"]: r for r in get_db().execute("SELECT * FROM race_box WHERE race_id = ?", (rid,))}
    rows, total, invalid_all, ballots_all = [], {c: 0 for c in ids}, 0, 0
    for box in boxes():
        tal = tal_all.get(box, {})
        t_votes, t_invalid, t_ballots = totals_from(tal)
        st = states.get(box)
        official = json.loads(st["official"]) if st and st["official"] else {}
        final = bool(st and st["status"] == "final")
        if final:
            votes = {c: official.get(str(c)) or 0 for c in ids}
            invalid = official.get("invalid") if official.get("invalid") is not None else t_invalid
            ballots = official.get("ballots") if official.get("ballots") is not None else t_ballots
        else:
            votes, invalid, ballots = {c: t_votes.get(c, 0) for c in ids}, t_invalid, t_ballots
        for c in ids:
            total[c] += votes.get(c, 0)
        invalid_all += invalid or 0
        ballots_all += ballots or 0
        rows.append({"box": box, "status": "final" if final else ("counting" if tal else "not started"), "votes": votes,
                     "invalid": invalid, "ballots": ballots, "photo": bool(st and st["photo"])})
    order = {c["id"]: i for i, c in enumerate(cands)}
    standing = sorted(cands, key=lambda c: (-total[c["id"]], order[c["id"]]))
    places = [{"c": c, "votes": total[c["id"]], "place": i + 1, "wins": i < race["seats"]} for i, c in enumerate(standing)]
    return {"race": race, "cands": cands, "rows": rows, "places": places, "invalid": invalid_all, "ballots": ballots_all,
            "top": max(total.values() or [0]), "final_boxes": sum(1 for r in rows if r["status"] == "final"),
            "started": any(r["status"] != "not started" for r in rows)}


# --- pages ----------------------------------------------------------------------------------------

@bp.route("/atoll")
@login_required
def home():
    """Atoll count accounts land on their race; everyone else who may see races gets the list."""
    if is_race_only():
        rid = g.user["race_id"]
        return redirect(url_for("racecount.sheet" if race_role() == "count" else "racecount.results", rid=rid))
    if t1_box() and not can_manage():
        first = get_db().execute("SELECT id FROM races WHERE active = 1 ORDER BY id").fetchone()
        if first:
            return redirect(url_for("racecount.sheet", rid=first[0]))
    races = get_db().execute("SELECT * FROM races ORDER BY active DESC, id").fetchall()
    visible = [r for r in races if can_watch(r["id"])]
    if not visible and not can_manage():
        abort(403)
    return render_template("race/home.html", races=visible, manage=can_manage())


@bp.route("/atoll/manage", methods=["GET", "POST"])
@login_required
def manage():
    """Races and their candidates (owner and admins; an atoll administrator their own race)."""
    if not can_manage():
        abort(403)
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        rid = request.form.get("race_id", type=int)
        if action in ("add_race", "t1_all_on", "t1_all_off") and not is_portal_admin():
            abort(403)  # adding races and handing out our box agents stay with the owner and admins
        if action in ("edit_race", "add_cand") and not can_manage(rid):
            abort(403)
        if action in ("edit_cand", "remove_cand", "photo", "photo_remove"):
            row = db.execute("SELECT race_id FROM race_candidates WHERE id = ?", (request.form.get("cand_id", type=int),)).fetchone()
            if row is None:
                abort(404)
            if not can_manage(row["race_id"]):
                abort(403)
        if action == "add_race":
            name = clean_text(request.form.get("name", ""))[:80]
            seats = request.form.get("seats", type=int) or 2
            mv = request.form.get("max_votes", type=int) or 2
            if not name:
                flash("Type the race's name.", "error")
            else:
                db.execute("INSERT INTO races (name, seats, max_votes, created_at) VALUES (?, ?, ?, ?)",
                           (name, max(1, min(seats, 20)), max(1, min(mv, 2)), now_iso()))
                audit.log("race_add", target=name, commit=False)
                flash(f"{name} added. Now add its candidates.", "success")
        elif action == "edit_race":
            race_or_404(rid)
            name = clean_text(request.form.get("name", ""))[:80]
            seats = request.form.get("seats", type=int) or 2
            mv = request.form.get("max_votes", type=int) or 2
            if name:
                db.execute("UPDATE races SET name = ?, seats = ?, max_votes = ?, active = ? WHERE id = ?",
                           (name, max(1, min(seats, 20)), max(1, min(mv, 2)), 1 if request.form.get("active") else 0, rid))
                flash("Saved.", "success")
        elif action in ("t1_all_on", "t1_all_off"):
            from .warroom import tier_sql

            n = db.execute(f"UPDATE users SET race_t1 = ? WHERE {tier_sql('t1')} AND deleted_at IS NULL",
                           (1 if action == "t1_all_on" else 0,)).rowcount
            audit.log("race_t1_all", detail={"on": action == "t1_all_on", "n": n}, commit=False)
            flash(f"{n} box agent{'s' if n != 1 else ''} " + ("now count the atoll races at their box." if action == "t1_all_on"
                                                              else "no longer count the atoll races."), "success")
        elif action == "add_cand":
            race_or_404(rid)
            name = clean_text(request.form.get("name", ""))[:80]
            if not name:
                flash("Type the candidate's name.", "error")
            else:
                db.execute("INSERT INTO race_candidates (race_id, ballot_no, name, place, position) VALUES (?, ?, ?, ?, ?)",
                           (rid, _ballot(request.form.get("ballot_no")), name,
                            clean_text(request.form.get("place", ""))[:80],
                            db.execute("SELECT COUNT(*) FROM race_candidates WHERE race_id = ?", (rid,)).fetchone()[0]))
                audit.log("race_candidate_add", target=name, detail={"race": rid}, commit=False)
                flash(f"{name} added.", "success")
        elif action in ("edit_cand", "remove_cand", "photo", "photo_remove"):
            cid = request.form.get("cand_id", type=int)
            cand = db.execute("SELECT * FROM race_candidates WHERE id = ?", (cid,)).fetchone() or abort(404)
            if action == "edit_cand":
                name = clean_text(request.form.get("name", ""))[:80]
                if name:
                    db.execute("UPDATE race_candidates SET name = ?, ballot_no = ?, place = ? WHERE id = ?",
                               (name, _ballot(request.form.get("ballot_no")), clean_text(request.form.get("place", ""))[:80], cid))
                    flash("Saved.", "success")
            elif action == "remove_cand":
                if db.execute("SELECT 1 FROM race_tally WHERE race_id = ? AND voided_at IS NULL AND (combo LIKE ? OR combo LIKE ?"
                              " OR combo = ?)", (cand["race_id"], f"p:{cid}-%", f"p:%-{cid}", f"s:{cid}")).fetchone():
                    flash("This candidate already has ballots counted: they can't be removed.", "error")
                else:
                    db.execute("DELETE FROM race_candidates WHERE id = ?", (cid,))
                    flash(f"{cand['name']} removed.", "success")
            elif action == "photo_remove":
                db.execute("UPDATE race_candidates SET photo_path = NULL WHERE id = ?", (cid,))
                flash("Photo removed.", "success")
            else:
                try:
                    name = _save_photo(request.files.get("photo"), f"rc{cid}", (600, 800))
                except ValueError as exc:
                    flash(str(exc), "error")
                else:
                    db.execute("UPDATE race_candidates SET photo_path = ? WHERE id = ?", (name, cid))
                    flash(f"Photo saved for {cand['name']}.", "success")
        else:
            abort(400)
        db.commit()
        return redirect(url_for("racecount.manage"))
    races = db.execute("SELECT * FROM races ORDER BY id").fetchall()
    if not is_portal_admin():
        races = [r for r in races if r["id"] == g.user["race_id"]]
    people = db.execute("SELECT id, username, race_role, race_id, race_box FROM users WHERE race_role != ''"
                        " AND deleted_at IS NULL ORDER BY username COLLATE NOCASE").fetchall()
    from .warroom import tier_sql

    t1 = db.execute(f"SELECT username, war_box, race_t1 FROM users WHERE {tier_sql('t1')} AND deleted_at IS NULL"
                    " ORDER BY username COLLATE NOCASE").fetchall()
    if not is_portal_admin():
        people = [p for p in people if p["race_id"] == g.user["race_id"]]
    return render_template("race/manage.html", races=races, cands={r["id"]: candidates(r["id"]) for r in races},
                           people=people, roles=ROLES, t1=t1 if is_portal_admin() else [], portal_admin=is_portal_admin())


def _ballot(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() and 0 < int(value) < 100 else None


def _save_photo(upload, prefix, size):
    from PIL import Image, ImageOps

    if not upload or not upload.filename:
        raise ValueError("Choose a photo.")
    blob = upload.read(MAX_PHOTO + 1)
    if len(blob) > MAX_PHOTO:
        raise ValueError("The photo must be 12 MB or smaller.")
    try:
        with Image.open(io.BytesIO(blob)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail(size)
            name = f"{prefix}-{secrets.token_hex(6)}.jpg"
            im.save(os.path.join(_dir(), name), "JPEG", quality=85)
    except Exception:
        raise ValueError("That file isn't a photo the portal can read (use JPG or PNG).")
    return name


@bp.route("/atoll/<int:rid>/candidate/<int:cid>.jpg")
@login_required
def candidate_photo(rid, cid):
    if not (may_count(rid) or can_manage()):
        abort(403)
    row = get_db().execute("SELECT photo_path FROM race_candidates WHERE id = ? AND race_id = ?", (cid, rid)).fetchone()
    if not row or not row["photo_path"]:
        abort(404)
    resp = send_from_directory(_dir(), row["photo_path"], max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@bp.route("/atoll/<int:rid>/count")
@login_required
def sheet(rid):
    race = race_or_404(rid)
    box = my_box(rid)
    watch = can_watch(rid)
    all_boxes = boxes() if watch else []
    if not box and all_boxes:
        box = all_boxes[0]
    cands = candidates(rid)
    tal = tally(rid, box).get(box, {})
    votes, invalid, ballots = totals_from(tal)
    row, official = box_state(rid, box)
    diffs = differences(votes, invalid, ballots, official, cands) if row and row["status"] == "final" else []
    return render_template("race/sheet.html", race=race, box=box, boxes=all_boxes, cands=cands, tal=tal, votes=votes,
                           invalid=invalid, ballots=ballots, row=row, official=official, watch=watch, diffs=diffs,
                           label=label)


def differences(votes, invalid, ballots, official, cands):
    out = []
    for c in cands:
        o = official.get(str(c["id"]))
        if o is not None and o != votes.get(c["id"], 0):
            out.append((label(c), o, votes.get(c["id"], 0)))
    if official.get("invalid") is not None and official["invalid"] != invalid:
        out.append(("Invalid / blank", official["invalid"], invalid))
    if official.get("ballots") is not None and official["ballots"] != ballots:
        out.append(("Ballots", official["ballots"], ballots))
    return out


@bp.post("/atoll/<int:rid>/tap")
@login_required
def tap(rid):
    """One ballot, or undo=1 for this person's last one at the box. Safe to resend (key)."""
    race = race_or_404(rid)
    box = my_box(rid)
    if not box:
        abort(400)
    db = get_db()
    row, _ = box_state(rid, box)
    if row and row["status"] == "final":
        return jsonify({"ok": False, "error": "This box is final. Ask for it to be reopened."}), 409
    key = (request.form.get("key") or "")[:64] or None
    if key and db.execute("SELECT 1 FROM race_tally WHERE client_key = ?", (key,)).fetchone():
        return jsonify({"ok": True, "repeat": True})
    if request.form.get("undo") == "1":
        last = db.execute("SELECT id FROM race_tally WHERE race_id = ? AND box = ? AND by_user = ? AND voided_at IS NULL"
                          " AND combo != 'undo' ORDER BY id DESC LIMIT 1", (rid, box, g.user["id"])).fetchone()
        if last:
            db.execute("UPDATE race_tally SET voided_at = ? WHERE id = ?", (now_iso(), last["id"]))
        db.execute("INSERT INTO race_tally (race_id, box, combo, at, by_user, client_key, voided_at)"
                   " VALUES (?, ?, 'undo', ?, ?, ?, ?)", (rid, box, now_iso(), g.user["id"], key, now_iso()))
    else:
        combo = request.form.get("combo", "")
        if combo not in {c[0] for c in combos(candidates(rid), race["max_votes"])}:
            abort(400)
        db.execute("INSERT INTO race_tally (race_id, box, combo, at, by_user, client_key) VALUES (?, ?, ?, ?, ?, ?)",
                   (rid, box, combo, now_iso(), g.user["id"], key))
    db.commit()
    tal = tally(rid, box).get(box, {})
    votes, invalid, ballots = totals_from(tal)
    return jsonify({"ok": True, "ballots": ballots, "invalid": invalid, "votes": {str(k): v for k, v in votes.items()},
                    "tally": tal})


@bp.post("/atoll/<int:rid>/final")
@login_required
def final(rid):
    race_or_404(rid)
    box = my_box(rid)
    if not box:
        abort(400)
    cands = candidates(rid)
    db = get_db()

    def num(name):
        value = (request.form.get(name) or "").strip()
        return int(value) if value.isdigit() and int(value) < 1_000_000 else None

    back = url_for("racecount.sheet", rid=rid, box=box if can_watch(rid) else None)
    official = {str(c["id"]): num(f"c{c['id']}") for c in cands}
    official["invalid"] = num("invalid")
    official["ballots"] = num("ballots")
    if any(official[str(c["id"])] is None for c in cands):
        flash("Enter every candidate's votes from the official sheet (0 if none).", "error")
        return redirect(back)
    photo = None
    upload = request.files.get("photo")
    if upload and upload.filename:
        try:
            photo = _save_photo(upload, "sheet", (2400, 2400))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(back)
    old, _ = box_state(rid, box)
    db.execute("INSERT INTO race_box (race_id, box, status, official, photo, final_at, final_by, note)"
               " VALUES (?, ?, 'final', ?, ?, ?, ?, ?) ON CONFLICT(race_id, box) DO UPDATE SET status = 'final',"
               " official = excluded.official, photo = COALESCE(excluded.photo, race_box.photo),"
               " final_at = excluded.final_at, final_by = excluded.final_by, note = excluded.note",
               (rid, box, json.dumps(official), photo or (old["photo"] if old else None), now_iso(), g.user["id"],
                (request.form.get("note") or "")[:500]))
    audit.log("race_final", target=box, detail={"race": rid, **official}, commit=False)
    db.commit()
    tal = tally(rid, box).get(box, {})
    diffs = differences(*totals_from(tal), official, cands) if tal else []
    flash("Box final. " + ("The official sheet and the tally differ: check the lines in red." if diffs else
                           "The official sheet matches the tally." if tal else "Official numbers saved."),
          "error" if diffs else "success")
    return redirect(back)


@bp.post("/atoll/<int:rid>/reopen")
@login_required
def reopen(rid):
    if not can_watch(rid):
        abort(403)
    box = (request.form.get("box") or "")[:200]
    get_db().execute("UPDATE race_box SET status = 'counting' WHERE race_id = ? AND box = ?", (rid, box))
    audit.log("race_reopen", target=box, detail={"race": rid}, commit=False)
    get_db().commit()
    flash(f"{box} reopened for counting.", "success")
    return redirect(url_for("racecount.sheet", rid=rid, box=box))


@bp.route("/atoll/<int:rid>/sheet-photo/<path:box>")
@login_required
def sheet_photo(rid, box):
    if not can_watch(rid) and not (race_role() == "count" and g.user["race_id"] == rid and g.user["race_box"] == box) \
            and not (t1_box() and t1_box() == box):
        abort(403)
    row, _ = box_state(rid, box)
    if not row or not row["photo"]:
        abort(404)
    resp = send_from_directory(_dir(), row["photo"], max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


def patterns(rid, box=None):
    """How ballots were filled in (from the counters' taps): each pair, each single vote and invalid, with the
    partners of every candidate. box=None: every box."""
    race = race_or_404(rid)
    cands = candidates(rid)
    tal_all = tally(rid, box)
    pattern = {}
    for b, tal in tal_all.items():
        for combo, n in tal.items():
            pattern[combo] = pattern.get(combo, 0) + n
    tallied = sum(pattern.values())
    partners = {c["id"]: {"alone": 0, "with": {}, "total": 0} for c in cands}
    for combo, n in pattern.items():
        if combo.startswith("p:"):
            a, b = (int(x) for x in combo[2:].split("-"))
            for me, other in ((a, b), (b, a)):
                if me in partners:
                    partners[me]["with"][other] = partners[me]["with"].get(other, 0) + n
                    partners[me]["total"] += n
        elif combo.startswith("s:") and int(combo[2:]) in partners:
            partners[int(combo[2:])]["alone"] += n
            partners[int(combo[2:])]["total"] += n
    rows = [(key, text, ids, pattern.get(key, 0)) for key, text, ids in combos(cands, race["max_votes"])]
    pairs = sum(n for key, _, _, n in rows if key.startswith("p:"))
    singles = sum(n for key, _, _, n in rows if key.startswith("s:"))
    return {"rows": rows, "tallied": tallied, "max": max([n for *_, n in rows] or [0]), "partners": partners,
            "pairs": pairs, "singles": singles, "invalid": pattern.get("x", 0), "box": box}


@bp.route("/atoll/<int:rid>/results", endpoint="results")
@login_required
def results_page(rid):
    if not can_watch(rid):
        abort(403)
    pbox = (request.args.get("pbox") or "")[:200]
    all_boxes = boxes()
    return render_template("race/results.html", r=results(rid), label=label, all_boxes=all_boxes, can_edit=can_manage(rid),
                           pt=patterns(rid, pbox if pbox in all_boxes else None), pbox=pbox if pbox in all_boxes else "")


@bp.route("/atoll/<int:rid>/results.xlsx")
@login_required
def results_xlsx(rid):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    if not can_watch(rid):
        abort(403)
    r = results(rid)
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ws.append([f"{r['race']['name']} · {r['final_boxes']} of {len(r['rows'])} boxes final · {stamp}"])
    ws["A1"].font = Font(bold=True)
    ws.append([])
    ws.append(["Place", "Candidate", "Votes"])
    for p in r["places"]:
        ws.append([p["place"], label(p["c"]), p["votes"]])
    ws.append(["", "Invalid / blank", r["invalid"]])
    ws.append(["", "Ballots", r["ballots"]])
    ws.append([])
    ws.append(["Box", "Status"] + [label(c) for c in r["cands"]] + ["Invalid", "Ballots"])
    for x in r["rows"]:
        ws.append([x["box"], x["status"]] + [x["votes"].get(c["id"], 0) for c in r["cands"]] + [x["invalid"], x["ballots"]])
    for row in (3, 8 + len(r["places"])):
        for cell in ws[row]:
            cell.font = Font(bold=True)
    pt = patterns(rid)
    pw = wb.create_sheet("Voting pattern")
    pw.append([f"{r['race']['name']} · voting pattern · {pt['tallied']} ballots tapped by the counters"])
    pw["A1"].font = Font(bold=True)
    pw.append([])
    pw.append(["Ballot", "Ballots", "% of ballots"])
    for key, text, ids, n in pt["rows"]:
        pw.append([text, n, round(100 * n / pt["tallied"], 1) if pt["tallied"] else 0])
    pw.append([])
    pw.append(["Candidate", "Their ballots", "Alone"] + [f"With {label(c)}" for c in r["cands"]])
    for c in r["cands"]:
        p = pt["partners"][c["id"]]
        pw.append([label(c), p["total"], p["alone"]] + [p["with"].get(o["id"], "") if o["id"] != c["id"] else "—"
                                                        for o in r["cands"]])
    for row in (3, 5 + len(pt["rows"])):
        for cell in pw[row]:
            cell.font = Font(bold=True)
    pw.column_dimensions["A"].width = 44
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 30
    out = io.BytesIO()
    wb.save(out)
    audit.log("race_results_export", detail={"race": rid})
    name = f"atoll-count-{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
    return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
