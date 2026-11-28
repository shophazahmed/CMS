"""Replace one box's island council count with the ballots from an Excel sheet (after a wrong count).

The sheet has one row per ballot: a header row with "Ballot" in the first column, one column per candidate
(a 1, x or ✓ under each candidate marked) and optionally a "status" column (blank / rejected ballots count as
invalid). Columns are matched to the portal's candidates by name (then by ballot order) and can be changed
before saving. Check shows the totals and patterns; Replace cancels every earlier tap at that box (kept as
undone, never deleted) and saves these ballots instead. The official result sheet, if entered, is kept.
"""
import csv
import difflib
import io

from flask import Blueprint, abort, flash, g, render_template, request

from . import audit, pledges
from .auth import login_required
from .db import get_db, now_iso

bp = Blueprint("countimport", __name__)
MAX_BYTES = 10 * 1024 * 1024
TICKS = {"1", "1.0", "x", "✓", "✔", "yes", "y", "true"}


def _rows(upload):
    blob = upload.read(MAX_BYTES + 1)
    if len(blob) > MAX_BYTES:
        raise ValueError("The file must be 10 MB or smaller.")
    name = (upload.filename or "").lower()
    if name.endswith(".csv"):
        return [list(r) for r in csv.reader(io.StringIO(blob.decode("utf-8-sig", "replace")))]
    if not name.endswith((".xlsx", ".xlsm")):
        raise ValueError("Use the Excel file (.xlsx or .xlsm) or a CSV.")
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)  # macros are never run
    except Exception:
        raise ValueError("The portal couldn't read this file.")
    for ws in wb.worksheets:
        rows = [["" if v is None else str(v).strip() for v in r] for r in ws.iter_rows(values_only=True)]
        if any(r and r[0].lower().startswith("ballot") for r in rows):
            return rows
    raise ValueError("No sheet with a 'Ballot' header row was found.")


def parse(upload):
    """{headers: [candidate column names], ballots: [[col index, ...] per ballot], invalid: n, bad: [row numbers]}"""
    rows = _rows(upload)
    head_at = next((i for i, r in enumerate(rows) if r and r[0].lower().startswith("ballot")), None)
    if head_at is None:
        raise ValueError("No header row starting with 'Ballot'.")
    head = rows[head_at]
    status_col = next((j for j, h in enumerate(head) if "status" in h.lower()), None)
    end = status_col if status_col is not None else len(head)
    cols = [j for j in range(1, end) if head[j] and "pattern" not in head[j].lower()]
    if len(cols) < 2:
        raise ValueError("The header row needs a column for each candidate.")
    names = {head[j].casefold() for j in cols}
    ballots, invalid, bad = [], 0, []
    for n, r in enumerate(rows[head_at + 1:], start=head_at + 2):
        r = r + [""] * (len(head) - len(r))
        cells = [r[j] for j in cols]
        if any(c.casefold() in names for c in cells if c):
            continue  # a repeated header row
        marked = [k for k, c in enumerate(cells) if c.casefold() in TICKS]
        status = (r[status_col] if status_col is not None else "").lower()
        if status and any(w in status for w in ("blank", "reject", "invalid", "spoil")):
            invalid += 1
            continue
        if not marked:
            if any(cells):
                bad.append(n)
            continue
        ballots.append(marked)
    return {"headers": [head[j] for j in cols], "ballots": ballots, "invalid": invalid, "bad": bad}


def auto_map(headers, cands):
    """Column -> candidate id: by name (short or full), then the closest spelling, then ballot order."""
    out, used = [], set()
    for k, h in enumerate(headers):
        key = h.casefold().strip()
        best = None
        for c in cands:
            names = [c["name"] or "", c["full_name"] or ""] if "full_name" in c.keys() else [c["name"] or ""]
            if c["id"] not in used and any(key == x.casefold() or (x and key in x.casefold().split()) for x in names):
                best = c
                break
        if best is None:
            scored = sorted(((difflib.SequenceMatcher(None, key, (c["name"] or "").casefold()).ratio(), c) for c in cands
                             if c["id"] not in used), key=lambda t: -t[0])
            if scored and scored[0][0] >= 0.6:
                best = scored[0][1]
        if best is None and k < len(cands) and cands[k]["id"] not in used:
            best = cands[k]
        out.append(best["id"] if best else None)
        if best:
            used.add(best["id"])
    return out


def build(parsed, mapping, cands, max_votes):
    """The tally records (combos) and a summary, or errors."""
    order = {c["id"]: i for i, c in enumerate(cands)}
    combos, errors, votes, pattern = [], [], {c["id"]: 0 for c in cands}, {}
    if None in mapping or len(set(mapping)) != len(mapping):
        errors.append("Match every column to a different candidate.")
        return combos, errors, votes, pattern
    for n, marked in enumerate(parsed["ballots"], 1):
        ids = sorted({mapping[k] for k in marked}, key=lambda i: order[i])
        if max_votes and len(ids) > max_votes:
            errors.append(f"Ballot {n} has {len(ids)} candidates (a ballot has at most {max_votes}).")
            continue
        combo = f"s:{ids[0]}" if len(ids) == 1 else "p:" + "-".join(str(i) for i in ids)
        combos.append(combo)
        pattern[combo] = pattern.get(combo, 0) + 1
        for i in ids:
            votes[i] += 1
    combos += ["x"] * parsed["invalid"]
    if parsed["invalid"]:
        pattern["x"] = parsed["invalid"]
    return combos, errors, votes, pattern


@bp.route("/count/import", methods=["GET", "POST"])
@login_required
def replace():
    from .votecount import _watch, box_state, candidates, combos as all_combos, tally, totals_from
    from .racecount import boxes as all_boxes

    if not _watch():
        abort(403)
    cands = candidates()
    boxes = all_boxes()
    box = (request.values.get("box") or "")[:200]
    ctx = {"boxes": boxes, "box": box, "cands": cands, "label": pledges.label, "result": None}
    if request.method == "GET":
        return render_template("count/import.html", **ctx)
    if box not in boxes:
        flash("Choose the box.", "error")
        return render_template("count/import.html", **ctx)
    upload = request.files.get("sheet")
    if not upload or not upload.filename:
        flash("Choose the Excel file.", "error")
        return render_template("count/import.html", **ctx)
    try:
        parsed = parse(upload)
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("count/import.html", **ctx)
    ids = {c["id"] for c in cands}
    mapping = auto_map(parsed["headers"], cands)
    for k in range(len(parsed["headers"])):
        v = request.form.get(f"map{k}", "")
        if v.isdigit() and int(v) in ids:
            mapping[k] = int(v)
    max_votes = pledges.max_votes() or 2
    records, errors, votes, pattern = build(parsed, mapping, cands, max_votes)
    before = totals_from(tally(box).get(box, {}))
    row, official = box_state(box)
    apply = request.form.get("apply") == "1" and not errors
    if apply:
        db = get_db()
        stamp = now_iso()
        n_old = db.execute("UPDATE count_tally SET voided_at = ? WHERE box = ? AND voided_at IS NULL AND combo != 'undo'",
                           (stamp, box)).rowcount
        db.executemany("INSERT INTO count_tally (box, combo, at, by_user) VALUES (?, ?, ?, ?)",
                       [(box, c, stamp, g.user["id"]) for c in records])
        audit.log("count_replace", target=box, detail={"file": upload.filename, "ballots": len(records),
                                                       "old_taps": n_old, "votes": {str(k): v for k, v in votes.items()}},
                  commit=False)
        db.commit()
        flash(f"{box}: the count now has the {len(records)} ballots from the sheet ({n_old} earlier taps cancelled).",
              "success")
    labels = {key: text for key, text, _ in all_combos(cands)}
    ctx.update(result={"parsed": parsed, "mapping": mapping, "errors": errors, "votes": votes, "records": len(records),
                       "pattern": sorted(pattern.items(), key=lambda kv: -kv[1]), "labels": labels, "applied": apply,
                       "before": before, "final": bool(row and row["status"] == "final"), "official": official,
                       "file": upload.filename})
    return render_template("count/import.html", **ctx)
