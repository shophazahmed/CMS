"""'100% confirmed' voters: the candidate's own supporters.

Private to the 'admin' account (auth.can_manage_confirmed_voters on every route). The list lives
in its own table, so no other page, export or role can show it unless it is joined on purpose.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from . import audit, campaign
from .auth import can_manage_confirmed_voters
from .db import get_db, now_iso

bp = Blueprint("confirmed", __name__)

MAX_NOTE = 300
# Search rows: whether the voter is confirmed (only added to the query for the 'admin' account)
CONFIRMED_COLUMN = ("EXISTS (SELECT 1 FROM confirmed_voters cv WHERE cv.national_id = voters.national_id)"
                    " AS confirmed")


def metrics():
    """Confirmed out of the campaign's voters (e.g. MDP), plus the same out of all voters."""
    db = get_db()
    clause, params = campaign.campaign_clause("v.party")
    scope = f" AND {clause}" if clause else ""
    join = "FROM voters v LEFT JOIN confirmed_voters c ON c.national_id = v.national_id"
    row = db.execute(
        f"SELECT COUNT(*) AS total, COUNT(c.national_id) AS confirmed {join} WHERE 1{scope}", params).fetchone()
    every = db.execute(f"SELECT COUNT(*) AS total, COUNT(c.national_id) AS confirmed {join}").fetchone()
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    this_week = db.execute(
        f"SELECT COUNT(*) {join} WHERE c.confirmed_at >= ?{scope}", [week] + params).fetchone()[0]
    pct = lambda n, d: round(100 * n / d, 1) if d else 0.0  # noqa: E731
    parties = campaign.visible_parties()
    return {
        "label": ", ".join("No party" if p == campaign.NO_PARTY else p for p in parties) if parties else "All voters",
        "total": row["total"], "confirmed": row["confirmed"], "pct": pct(row["confirmed"], row["total"]),
        "all_total": every["total"], "all_confirmed": every["confirmed"],
        "all_pct": pct(every["confirmed"], every["total"]), "this_week": this_week,
    }


def by_house():
    clause, params = campaign.campaign_clause("v.party")
    scope = f" AND {clause}" if clause else ""
    return get_db().execute(
        "SELECT v.house_name AS house, COUNT(*) AS total, COUNT(c.national_id) AS confirmed"
        " FROM voters v LEFT JOIN confirmed_voters c ON c.national_id = v.national_id"
        f" WHERE v.house_name != ''{scope} GROUP BY v.house_name"
        " ORDER BY COUNT(c.national_id) DESC, COUNT(*) DESC, v.house_name COLLATE NOCASE", params).fetchall()


@bp.post("/voters/<national_id>/confirm")
@can_manage_confirmed_voters
def set_confirmed(national_id):
    """Mark (confirmed=1) or unmark (confirmed=0). Takes the wanted state, not a toggle, so a
    double tap on a slow phone can't undo itself."""
    from .voters import get_voter_or_404, _safe_back

    get_voter_or_404(national_id)
    wanted = request.form.get("confirmed")
    if wanted not in ("0", "1"):
        abort(400)
    db = get_db()
    current = db.execute("SELECT note FROM confirmed_voters WHERE national_id = ?", (national_id,)).fetchone()
    note = request.form.get("note")
    note = note.replace("\r\n", "\n").strip()[:MAX_NOTE] if note is not None else (current["note"] if current else "")
    changed = False
    if wanted == "1":
        if current is None:
            db.execute("INSERT INTO confirmed_voters (national_id, confirmed_at, note) VALUES (?, ?, ?)",
                       (national_id, now_iso(), note))
            audit.log("voter_confirm", target=national_id, commit=False)
            changed = True
        elif note != current["note"]:
            db.execute("UPDATE confirmed_voters SET note = ? WHERE national_id = ?", (note, national_id))
            changed = True
    elif current is not None:
        db.execute("DELETE FROM confirmed_voters WHERE national_id = ?", (national_id,))
        audit.log("voter_unconfirm", target=national_id, commit=False)
        changed = True
    db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        m = metrics()
        return {"ok": True, "confirmed": wanted == "1", "changed": changed,
                "metrics": {k: m[k] for k in ("confirmed", "total", "pct")}}
    flash(("Marked as 100% confirmed." if wanted == "1" else "No longer confirmed.") if changed else "Nothing changed.",
          "success" if changed else "info")
    return redirect(_safe_back(request.form.get("back")) if request.form.get("back") else
                    url_for("voters.detail", national_id=national_id))


@bp.route("/confirmed")
@can_manage_confirmed_voters
def overview():
    return render_template("confirmed/overview.html", m=metrics(), houses=by_house())


@bp.route("/confirmed/metrics.json")
@can_manage_confirmed_voters
def metrics_json():
    resp = metrics()
    return resp
