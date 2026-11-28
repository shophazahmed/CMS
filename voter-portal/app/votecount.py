"""Vote count: election night, box by box.

Box agents (War Room tier 1) tap each ballot as it is read out: a pair of candidates, a single vote,
or invalid / blank. That gives live totals and the voting pattern (which pairs voters chose). When the
count ends they enter the official result sheet (and a photo of it); the portal compares it with the
tally and marks the box final. The results page (owner and War Room viewers) shows the totals, the
projected seats, every box, the pattern and pledges against actual votes.

Like the War Room: taps made without signal wait on the phone and are sent later (each has a key, so it
counts once); box agents work only on their own box, only while the War Room is open.
"""
import io
import json
import os
import secrets
from datetime import datetime, timezone

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from . import audit, pledges
from .auth import login_required
from .db import get_db, now_iso

bp = Blueprint("votecount", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS count_tally (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    box        TEXT NOT NULL,
    combo      TEXT NOT NULL,          -- p:<a>-<b> (a pair), s:<a> (one vote), x (invalid / blank)
    at         TEXT NOT NULL,
    by_user    INTEGER,
    client_key TEXT,
    voided_at  TEXT                    -- undone
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_count_key ON count_tally (client_key);
CREATE INDEX IF NOT EXISTS ix_count_box ON count_tally (box, voided_at);
CREATE TABLE IF NOT EXISTS count_box (
    box        TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'counting' CHECK (status IN ('counting', 'final')),
    official   TEXT NOT NULL DEFAULT '{}',   -- {"<candidate id>": votes, "invalid": n, "ballots": n}
    photo      TEXT,
    final_at   TEXT,
    final_by   INTEGER,
    note       TEXT NOT NULL DEFAULT ''
);
"""
MAX_PHOTO = 12 * 1024 * 1024


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _dir():
    path = current_app.config.get("COUNT_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(current_app.config["CAMPAIGN_DIR"])), "count")
    os.makedirs(path, exist_ok=True)
    return path


# --- access --------------------------------------------------------------------------------------

def _tier():
    from .warroom import tier

    return tier()


def _watch():
    return _tier() in ("owner", "room")


def _my_box():
    """The box this request works on: a box agent's own box, or ?box= for the War Room."""
    from .warroom import is_open

    t = _tier()
    if t == "t1":
        if not is_open():
            abort(423)
        return g.user["war_box"] or ""
    if t in ("owner", "room"):
        return (request.values.get("box") or "")[:200]
    abort(403)


def candidates():
    """Candidates in ballot order (as on the ballot paper)."""
    cands = pledges.candidates()
    return sorted(cands, key=lambda c: (c["ballot_no"] is None, c["ballot_no"] or 0, c["position"]))


def combos(cands):
    """Every way a ballot can be filled in: (key, label, candidate ids)."""
    out = []
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            out.append((f"p:{a['id']}-{b['id']}", f"{pledges.label(a)} + {pledges.label(b)}", (a["id"], b["id"])))
    for a in cands:
        out.append((f"s:{a['id']}", f"{pledges.label(a)} only", (a["id"],)))
    out.append(("x", "Invalid / blank", ()))
    return out


# --- counting ------------------------------------------------------------------------------------

def tally(box=None):
    """{box: {combo: n}} of live (not undone) taps."""
    db = get_db()
    sql, params = "SELECT box, combo, COUNT(*) FROM count_tally WHERE voided_at IS NULL", []
    if box is not None:
        sql, params = sql + " AND box = ?", [box]
    out = {}
    for b, combo, n in db.execute(sql + " GROUP BY box, combo", params):
        out.setdefault(b, {})[combo] = n
    return out


def totals_from(tal):
    """Votes per candidate, invalid and ballots from one box's tally."""
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


def box_state(box):
    row = get_db().execute("SELECT * FROM count_box WHERE box = ?", (box,)).fetchone()
    official = json.loads(row["official"]) if row and row["official"] else {}
    return row, official


@bp.route("/count")
@login_required
def sheet():
    box = _my_box()
    cands = candidates()
    db = get_db()
    boxes = [] if _tier() == "t1" else [r[0] for r in db.execute(
        "SELECT DISTINCT location FROM voters WHERE location != '' ORDER BY location")]
    if not box and boxes:
        box = boxes[0]
    tal = tally(box).get(box, {})
    votes, invalid, ballots = totals_from(tal)
    row, official = box_state(box)
    voted = db.execute("SELECT COUNT(*) FROM war_voted w JOIN voters v ON v.national_id = w.national_id"
                       " WHERE v.location = ?", (box,)).fetchone()[0]
    registered = db.execute("SELECT COUNT(*) FROM voters WHERE location = ? AND deceased = 0", (box,)).fetchone()[0]
    return render_template("count/sheet.html", box=box, boxes=boxes, cands=cands, combos=combos(cands), tal=tal,
                           votes=votes, invalid=invalid, ballots=ballots, row=row, official=official, voted=voted,
                           registered=registered, watch=_watch(), label=pledges.label, max_votes=pledges.max_votes() or 2,
                           diffs=differences(votes, invalid, ballots, official, cands) if row and row["status"] == "final" else [],
                           counters=counted_by("count_tally", box) if _watch() else [])


@bp.post("/count/tap")
@login_required
def tap():
    """One ballot (combo), or undo=1 to take back this agent's last tap at the box. Safe to resend (key)."""
    box = _my_box()
    if not box:
        abort(400)
    db = get_db()
    row, _ = box_state(box)
    if row and row["status"] == "final":
        return jsonify({"ok": False, "error": "This box is final. Ask the War Room to reopen it."}), 409
    key = (request.form.get("key") or "")[:64] or None
    if key and db.execute("SELECT 1 FROM count_tally WHERE client_key = ?", (key,)).fetchone():
        return jsonify({"ok": True, "repeat": True})
    if request.form.get("undo") == "1":
        last = db.execute("SELECT id FROM count_tally WHERE box = ? AND by_user = ? AND voided_at IS NULL"
                          " ORDER BY id DESC LIMIT 1", (box, g.user["id"])).fetchone()
        if last:
            db.execute("UPDATE count_tally SET voided_at = ? WHERE id = ?", (now_iso(), last["id"]))
        # remember the key so a resent undo doesn't take back a second ballot
        db.execute("INSERT INTO count_tally (box, combo, at, by_user, client_key, voided_at) VALUES (?, 'undo', ?, ?, ?, ?)",
                   (box, now_iso(), g.user["id"], key, now_iso()))
    else:
        combo = request.form.get("combo", "")
        if combo not in {c[0] for c in combos(candidates())}:
            abort(400)
        db.execute("INSERT INTO count_tally (box, combo, at, by_user, client_key) VALUES (?, ?, ?, ?, ?)",
                   (box, combo, now_iso(), g.user["id"], key))
    db.commit()
    tal = tally(box).get(box, {})
    votes, invalid, ballots = totals_from(tal)
    return jsonify({"ok": True, "ballots": ballots, "invalid": invalid, "votes": {str(k): v for k, v in votes.items()},
                    "tally": tal})


def differences(votes, invalid, ballots, official, cands):
    """Where the official sheet and the tally disagree: [(label, official, tally)]."""
    out = []
    for c in cands:
        o = official.get(str(c["id"]))
        if o is not None and o != votes.get(c["id"], 0):
            out.append((pledges.label(c), o, votes.get(c["id"], 0)))
    if official.get("invalid") is not None and official["invalid"] != invalid:
        out.append(("Invalid / blank", official["invalid"], invalid))
    if official.get("ballots") is not None and official["ballots"] != ballots:
        out.append(("Ballots", official["ballots"], ballots))
    return out


@bp.post("/count/final")
@login_required
def final():
    """The official result sheet: numbers per candidate, invalid, ballots, and a photo."""
    box = _my_box()
    if not box:
        abort(400)
    cands = candidates()
    db = get_db()

    def num(name):
        value = (request.form.get(name) or "").strip()
        return int(value) if value.isdigit() and int(value) < 1_000_000 else None

    official = {str(c["id"]): num(f"c{c['id']}") for c in cands}
    official["invalid"] = num("invalid")
    official["ballots"] = num("ballots")
    if any(official[str(c["id"])] is None for c in cands):
        flash("Enter every candidate's votes from the official sheet (0 if none).", "error")
        return redirect(url_for("votecount.sheet", box=box if _watch() else None))
    photo = None
    upload = request.files.get("photo")
    if upload and upload.filename:
        blob = upload.read(MAX_PHOTO + 1)
        if len(blob) > MAX_PHOTO:
            flash("The photo must be 12 MB or smaller.", "error")
            return redirect(url_for("votecount.sheet", box=box if _watch() else None))
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(blob))
            img.verify()
            img = Image.open(io.BytesIO(blob)).convert("RGB")
            img.thumbnail((2400, 2400))
            photo = f"{secrets.token_hex(8)}.jpg"
            img.save(os.path.join(_dir(), photo), "JPEG", quality=85)
        except Exception:
            flash("That file isn't a photo the portal can read. Try a JPG or PNG.", "error")
            return redirect(url_for("votecount.sheet", box=box if _watch() else None))
    old, _ = box_state(box)
    db.execute("INSERT INTO count_box (box, status, official, photo, final_at, final_by, note) VALUES (?, 'final', ?, ?, ?, ?, ?)"
               " ON CONFLICT(box) DO UPDATE SET status = 'final', official = excluded.official,"
               " photo = COALESCE(excluded.photo, count_box.photo), final_at = excluded.final_at,"
               " final_by = excluded.final_by, note = excluded.note",
               (box, json.dumps(official), photo or (old["photo"] if old else None), now_iso(), g.user["id"],
                (request.form.get("note") or "")[:500]))
    audit.log("count_final", target=box, detail=official, commit=False)
    db.commit()
    tal = tally(box).get(box, {})
    votes, invalid, ballots = totals_from(tal)
    diffs = differences(votes, invalid, ballots, official, cands) if tal else []
    flash("Box final. " + ("The official sheet and the tally differ: check the lines in red." if diffs else
                           "The official sheet matches the tally." if tal else "Official numbers saved."),
          "error" if diffs else "success")
    return redirect(url_for("votecount.sheet", box=box if _watch() else None))


@bp.route("/count/export.<fmt>")
@login_required
def export(fmt):
    """One box's count as CSV / Excel: every ballot type, the votes per candidate, and the official sheet once
    the box is final. Box agents get their own box; the War Room any box."""
    import csv

    from openpyxl import Workbook
    from openpyxl.styles import Font

    if fmt not in ("csv", "xlsx"):
        abort(404)
    box = _my_box()
    if not box:
        abort(400)
    cands = candidates()
    tal = tally(box).get(box, {})
    votes, invalid, ballots = totals_from(tal)
    row, official = box_state(box)
    final = bool(row and row["status"] == "final")
    tallied = sum(tal.values())
    pattern = [["Ballot", "Ballots", "% of tallied"]] + [
        [text, tal.get(key, 0), round(100 * tal.get(key, 0) / tallied, 1) if tallied else 0]
        for key, text, _ in combos(cands)] + [["Total", tallied, 100 if tallied else 0]]
    summary = [["Candidate", "Votes (tally)"] + (["Official sheet", "Difference"] if final else [])]
    for c in cands:
        line = [pledges.label(c), votes.get(c["id"], 0)]
        if final:
            o = official.get(str(c["id"]))
            line += [o, (o - votes.get(c["id"], 0)) if o is not None else ""]
        summary.append(line)
    for name, t_val, o_key in (("Invalid / blank", invalid, "invalid"), ("Ballots", ballots, "ballots")):
        line = [name, t_val]
        if final:
            o = official.get(o_key)
            line += [o if o is not None else "", (o - t_val) if o is not None else ""]
        summary.append(line)
    audit.log("count_export", target=box, detail={"fmt": fmt})
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    safe = "".join(ch if ch.isalnum() else "-" for ch in box)[:40]
    name = f"count-{safe}-{stamp}.{fmt}"
    title = f"Vote count · {box} · {'final' if final else 'counting'} · {datetime.now().strftime('%-d %b %Y %H:%M')}"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([title])
        w.writerow([])
        w.writerows(summary)
        w.writerow([])
        w.writerows(pattern)
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    wb = Workbook()
    ws = wb.active
    ws.title = "Count"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    for line in summary:
        ws.append(line)
    ws.append([])
    for line in pattern:
        ws.append(line)
    for r in (3, 3 + len(summary) + 1):
        for cell in ws[r]:
            cell.font = Font(bold=True)
    ws.column_dimensions["A"].width = 34
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.post("/count/reopen")
@login_required
def reopen():
    if not _watch():
        abort(403)
    box = (request.form.get("box") or "")[:200]
    get_db().execute("UPDATE count_box SET status = 'counting' WHERE box = ?", (box,))
    audit.log("count_reopen", target=box, commit=False)
    get_db().commit()
    flash(f"{box} reopened for counting.", "success")
    return redirect(url_for("votecount.sheet", box=box))


@bp.route("/count/photo/<box>")
@login_required
def photo(box):
    if not _watch() and not (_tier() == "t1" and g.user["war_box"] == box):
        abort(403)
    row, _ = box_state(box)
    if not row or not row["photo"]:
        abort(404)
    resp = send_from_directory(_dir(), row["photo"], max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# --- results -------------------------------------------------------------------------------------

def results(only=None):
    """The count, all boxes (or only=one box)."""
    from . import superpledge

    db = get_db()
    cands = candidates()
    ids = [c["id"] for c in cands]
    tal_all = tally()
    states = {r["box"]: r for r in db.execute("SELECT * FROM count_box")}
    boxes = [r[0] for r in db.execute("SELECT location FROM voters WHERE location != '' AND deceased = 0"
                                      " GROUP BY location ORDER BY COUNT(*) DESC")]
    all_boxes = list(boxes)
    if only:
        boxes = [b for b in boxes if b == only]
    voted = dict(db.execute("SELECT v.location, COUNT(*) FROM war_voted w JOIN voters v ON v.national_id = w.national_id"
                            " GROUP BY v.location").fetchall())
    registered = dict(db.execute("SELECT location, COUNT(*) FROM voters WHERE deceased = 0 GROUP BY location").fetchall())
    expected = {}
    for box, cid, n in db.execute("SELECT v.location, p.candidate_id, COUNT(*) FROM pledges p JOIN voters v"
                                  " ON v.national_id = p.national_id WHERE p.value = 'yes' AND v.deceased = 0"
                                  " AND v.national_id NOT IN (SELECT national_id FROM pledge_noshow)"
                                  " GROUP BY v.location, p.candidate_id"):
        expected.setdefault(box, {})[cid] = n
    rows, total_votes, total_invalid, total_ballots, pattern = [], {c: 0 for c in ids}, 0, 0, {}
    for box in boxes:
        tal = tal_all.get(box, {})
        t_votes, t_invalid, t_ballots = totals_from(tal)
        st = states.get(box)
        official = json.loads(st["official"]) if st and st["official"] else {}
        is_final = bool(st and st["status"] == "final")
        if is_final:  # the official sheet counts once a box is final
            votes = {c: official.get(str(c)) or 0 for c in ids}
            invalid = official.get("invalid") if official.get("invalid") is not None else t_invalid
            ballots = official.get("ballots") if official.get("ballots") is not None else t_ballots
        else:
            votes, invalid, ballots = {c: t_votes.get(c, 0) for c in ids}, t_invalid, t_ballots
        for combo, n in tal.items():
            pattern[combo] = pattern.get(combo, 0) + n
        for c in ids:
            total_votes[c] += votes.get(c, 0)
        total_invalid += invalid or 0
        total_ballots += ballots or 0
        rows.append({"box": box, "status": "final" if is_final else ("counting" if tal else "not started"),
                     "votes": votes, "invalid": invalid, "ballots": ballots, "voted": voted.get(box, 0),
                     "registered": registered.get(box, 0), "expected": expected.get(box, {}),
                     "photo": bool(st and st["photo"]),
                     "diffs": differences(t_votes, t_invalid, t_ballots, official, cands) if is_final and tal else []})
    ours = pledges.our_candidate()
    seats = superpledge.seats()
    order = {c["id"]: i for i, c in enumerate(cands)}
    standing = sorted(cands, key=lambda c: (-total_votes[c["id"]], order[c["id"]]))
    places = [{"c": c, "votes": total_votes[c["id"]], "place": i + 1, "wins": i < seats} for i, c in enumerate(standing)]
    verdict = None
    mine = next((p for p in places if p["c"]["id"] == ours), None)
    if mine and len(places) > seats:
        if mine["wins"]:
            verdict = {"wins": True, "margin": mine["votes"] - places[seats]["votes"], "vs": places[seats]["c"]["name"]}
        else:
            verdict = {"wins": False, "margin": places[seats - 1]["votes"] - mine["votes"] + 1,
                       "vs": places[seats - 1]["c"]["name"]}
    tallied = sum(pattern.values())
    with_ours = {}
    if ours:
        for combo, n in pattern.items():
            if combo.startswith("p:") and str(ours) in combo[2:].split("-"):
                other = [x for x in combo[2:].split("-") if x != str(ours)][0]
                with_ours[int(other)] = with_ours.get(int(other), 0) + n
    return {"cands": cands, "rows": rows, "places": places, "verdict": verdict, "top": max(total_votes.values() or [0]),
            "invalid": total_invalid, "ballots": total_ballots, "pattern": pattern, "tallied": tallied,
            "combos": combos(cands), "ours": ours, "with_ours": with_ours, "seats": seats,
            "final_boxes": sum(1 for r in rows if r["status"] == "final"), "boxes": len(rows),
            "expected": {c: sum(expected.get(b, {}).get(c, 0) for b in boxes) for c in ids},
            "all_boxes": all_boxes, "only": only if boxes else None,
            "now": (datetime.now(timezone.utc)).isoformat(timespec="seconds")}


@bp.route("/count/results")
@login_required
def results_page():
    if not _watch():
        abort(403)
    only = (request.args.get("box") or "")[:200] or None
    return render_template("count/results.html", r=results(only), label=pledges.label)


@bp.route("/count/results.xlsx")
@login_required
def results_xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Font

    if not _watch():
        abort(403)
    r = results()
    cands = r["cands"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.append(["Vote count", f"{r['final_boxes']} of {r['boxes']} boxes final", datetime.now().strftime("%-d %b %Y %H:%M")])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Place", "Candidate", "Votes", "Wins a seat", "Yes pledges (not no-show)"])
    for p in r["places"]:
        ws.append([p["place"], pledges.label(p["c"]), p["votes"], "yes" if p["wins"] else "", r["expected"].get(p["c"]["id"], 0)])
    ws.append(["", "Invalid / blank", r["invalid"]])
    ws.append(["", "Ballots", r["ballots"]])
    boxes = wb.create_sheet("By box")
    boxes.append(["Box", "Status", "Registered", "Marked voted", "Ballots"] + [pledges.label(c) for c in cands]
                 + ["Invalid / blank"] + [f"{c['name']} Yes pledges" for c in cands])
    for row in r["rows"]:
        boxes.append([row["box"], row["status"], row["registered"], row["voted"], row["ballots"]]
                     + [row["votes"].get(c["id"], 0) for c in cands] + [row["invalid"]]
                     + [row["expected"].get(c["id"], 0) for c in cands])
    pat = wb.create_sheet("Voting pattern")
    pat.append(["Ballot", "Ballots", "% of tallied"])
    for key, label, _ in r["combos"]:
        n = r["pattern"].get(key, 0)
        pat.append([label, n, round(100 * n / r["tallied"], 1) if r["tallied"] else 0])
    for sheet in wb.worksheets:
        for cell in sheet[1 if sheet.title != "Results" else 3]:
            cell.font = Font(bold=True)
        sheet.column_dimensions["A"].width = 30
        sheet.column_dimensions["B"].width = 22
    out = io.BytesIO()
    wb.save(out)
    audit.log("count_export")
    return Response(out.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="vote-count-{datetime.now().strftime("%Y%m%d-%H%M")}.xlsx"'})


def counted_by(table, box, extra_where="", params=()):
    """Who entered ballots at this box: [{name, ballots (live), undone, first, last}], most first. Imported sheets
    and taps both count; an undo record itself isn't a ballot."""
    from .warroom import _hm

    rows = get_db().execute(
        f"SELECT t.by_user AS uid, u.username AS name, SUM(t.voided_at IS NULL) AS live, SUM(t.voided_at IS NOT NULL) AS undone,"
        f" MIN(t.at) AS first, MAX(t.at) AS last FROM {table} t LEFT JOIN users u ON u.id = t.by_user"
        f" WHERE t.box = ? AND t.combo != 'undo'{extra_where} GROUP BY t.by_user ORDER BY live DESC",
        (box,) + tuple(params)).fetchall()
    return [{"uid": r["uid"], "name": r["name"] or "(removed account)", "ballots": r["live"] or 0, "undone": r["undone"] or 0,
             "first": _hm(r["first"]), "last": _hm(r["last"])} for r in rows]


def remove_counter(table, box, uid, extra_where="", params=()):
    """Cancel every ballot one account entered at this box (kept as undone). Returns how many."""
    cond = "by_user IS NULL" if uid is None else "by_user = ?"
    args = [now_iso(), box] + ([] if uid is None else [uid]) + list(params)
    return get_db().execute(f"UPDATE {table} SET voided_at = ? WHERE box = ? AND {cond} AND voided_at IS NULL"
                            f" AND combo != 'undo'{extra_where}", args).rowcount


@bp.post("/count/remove-counter")
@login_required
def remove_counter_route():
    """The War Room deletes one account's ballots at a box (a counter who counted wrongly)."""
    if not _watch():
        abort(403)
    box = (request.form.get("box") or "")[:200]
    raw = request.form.get("user_id", "")
    uid = int(raw) if raw.isdigit() else None
    n = remove_counter("count_tally", box, uid)
    who = get_db().execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone() if uid else None
    audit.log("count_remove_counter", target=box, detail={"user": uid, "ballots": n}, commit=False)
    get_db().commit()
    flash(f"{n} ballot{'s' if n != 1 else ''} entered by {who[0] if who else 'that account'} at {box} deleted from the count.",
          "success")
    return redirect(url_for("votecount.sheet", box=box))
