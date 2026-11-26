"""The public results page: a secret link the owner shares with anyone (no sign-in).

It shows only totals: voters and turnout across all boxes and box by box, and the vote count per
candidate and box. Nothing else from the portal: no names, ID numbers, phones, pledges, supporter
groups, agents, call or transport data, and no projection. The owner makes, replaces or stops the link
on the War Room team page; a stopped or replaced link shows "not available".
"""
import hmac
import json
import secrets
from datetime import datetime, timezone

from flask import Blueprint, abort, render_template

from . import audit, campaign
from .db import get_db

bp = Blueprint("publicboard", __name__)

LOCAL_OFFSET_H = 5  # Maldives time


def token():
    return campaign.get_setting("public_board_token") or ""


def show_count():
    return campaign.get_setting("public_board_count", "1") == "1"


def make_token():
    t = secrets.token_urlsafe(18)
    campaign.put_setting("public_board_token", t)
    audit.log("public_board_link", commit=False)
    return t


def stop():
    campaign.put_setting("public_board_token", "")
    audit.log("public_board_stop", commit=False)


def turnout():
    """[{box, registered, voted, pct}] and overall totals. A box's voters are its official list once loaded."""
    from .warroom import OFF_LIST, box_expr

    db = get_db()
    rows = []
    for box, n, voted in db.execute(
            f"SELECT {box_expr('voters')} AS b, COUNT(*), SUM(national_id IN (SELECT national_id FROM war_voted))"
            " FROM voters WHERE deceased = 0 GROUP BY b ORDER BY b COLLATE NOCASE"):
        if not box or box == OFF_LIST:
            continue  # voters with no box (or left off the official lists) aren't part of the public count
        rows.append({"box": box, "registered": n, "voted": voted or 0, "pct": round(100 * (voted or 0) / n, 1) if n else 0})
    registered = sum(r["registered"] for r in rows)
    voted = sum(r["voted"] for r in rows)
    return rows, {"registered": registered, "voted": voted, "pct": round(100 * voted / registered, 1) if registered else 0}


def count():
    """The vote count as the public sees it: votes per candidate (ballot order and standing) and per box."""
    from .votecount import candidates, results

    r = results()
    cands = candidates()
    boxes = [{"box": x["box"], "status": x["status"], "votes": x["votes"], "invalid": x["invalid"], "ballots": x["ballots"]}
             for x in r["rows"]]
    places = [{"name": p["c"]["name"], "ballot_no": p["c"]["ballot_no"], "votes": p["votes"], "place": p["place"]}
              for p in r["places"]]
    return {"cands": [{"id": c["id"], "name": c["name"], "ballot_no": c["ballot_no"]} for c in cands], "boxes": boxes,
            "places": places, "invalid": r["invalid"], "ballots": r["ballots"], "final_boxes": r["final_boxes"],
            "total_boxes": r["boxes"], "started": any(b["status"] != "not started" for b in boxes)}


@bp.route("/live/<code>")
def board(code):
    want = token()
    if not want or not hmac.compare_digest(code.encode(), want.encode()):
        return render_template("public/closed.html"), 404
    rows, overall = turnout()
    from datetime import timedelta

    now = (datetime.now(timezone.utc) + timedelta(hours=LOCAL_OFFSET_H)).strftime("%-d %b %Y, %H:%M")
    resp = render_template("public/board.html", rows=rows, overall=overall, c=count() if show_count() else None,
                           now=now, title=campaign.get_setting("public_board_title") or "Election results")
    from flask import make_response

    out = make_response(resp)
    out.headers["Cache-Control"] = "no-store"
    out.headers["Referrer-Policy"] = "no-referrer"
    return out
