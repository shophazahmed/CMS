"""Super pledge voters and voting scenarios.

Super pledge voters vote the way a candidate decides. The super pledge sheet lists every voter, like the
pledge sheet, with a cell per candidate: marking it makes the voter that candidate's super voter (up to the
vote limit, e.g. 2 candidates). The scenarios page
projects the result (top seats win) from the pledges, the super voters' decisions and a few what-ifs:
some candidates' supporters cast only one vote, how Maybe and undecided voters go, and turnout.
Access is given by the owner, like the pledge sheet, and needs pledge sheet access too.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import audit, campaign, pledges
from .auth import can_mark_super, super_marker_required, super_viewer_required
from .db import get_db, now_iso

bp = Blueprint("superpledge", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS super_voters (
    national_id TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    by_user     INTEGER
);
-- Which candidates' super voters each voter is (they vote as that candidate decides).
-- super_voters is from the first version and no longer used.
CREATE TABLE IF NOT EXISTS super_votes (
    national_id  TEXT NOT NULL,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    at           TEXT NOT NULL,
    by_user      INTEGER,
    PRIMARY KEY (national_id, candidate_id)
);
"""
DEFAULT_SEATS = 2


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def seats():
    value = campaign.get_setting("seats")
    return int(value) if value and str(value).isdigit() and int(value) > 0 else DEFAULT_SEATS


def _back(default):
    back = request.form.get("back") or ""
    return redirect(back if back.startswith("/") and not back.startswith("//") else default)


def _by_ballot(cands):
    return sorted(cands, key=lambda c: (c["ballot_no"] is None, c["ballot_no"] or 0, c["position"]))


# --- the super pledge sheet ---------------------------------------------------------------------

def _super_map(ids):
    out = {}
    db = get_db()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for nid, cid in db.execute(f"SELECT national_id, candidate_id FROM super_votes WHERE national_id IN"
                                   f" ({','.join('?' * len(chunk))})", chunk):
            out.setdefault(nid, set()).add(cid)
    return out


@bp.route("/super")
@super_viewer_required
def sheet():
    """Every voter, like the pledge sheet; a cell per candidate marks the voter as that candidate's super voter."""
    f, where, params, cands, pc, pv = pledges._filters()
    cands = _by_ballot(cands)
    ids = {str(c["id"]) for c in cands}
    sc = request.args.get("sc", "")
    sc = sc if sc in ids or sc in ("any", "none") else ""
    if sc:
        cond = {"any": "national_id IN (SELECT national_id FROM super_votes)",
                "none": "national_id NOT IN (SELECT national_id FROM super_votes)"}.get(
            sc, "national_id IN (SELECT national_id FROM super_votes WHERE candidate_id = ?)")
        where = f"{where} AND {cond}" if where else f" WHERE {cond}"
        params = list(params) + ([int(sc)] if sc.isdigit() else [])
    sort_key, sort_dir, order = pledges.sheet_order()
    db = get_db()
    size = request.args.get("n", type=int)
    size = size if size in pledges.PAGE_SIZES else 50
    total = db.execute(f"SELECT COUNT(*) FROM voters{where}", params).fetchone()[0]
    pages = max(1, -(-total // size))
    try:
        page = min(max(1, int(request.args.get("page", 1))), pages)
    except ValueError:
        page = 1
    rows = db.execute(f"SELECT * FROM voters{where} ORDER BY {order} LIMIT ? OFFSET ?",
                      params + [size, (page - 1) * size]).fetchall()
    sub = f"SELECT national_id FROM voters{where}"
    per_cand = dict(db.execute(f"SELECT candidate_id, COUNT(*) FROM super_votes WHERE national_id IN ({sub})"
                               " GROUP BY candidate_id", params).fetchall())
    n_super = db.execute(f"SELECT COUNT(DISTINCT national_id) FROM super_votes WHERE national_id IN ({sub})",
                         params).fetchone()[0]
    w, p = pledges._scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template("super/sheet.html", rows=rows, votes=_super_map([r["national_id"] for r in rows]),
                           cands=cands, per_cand=per_cand, n_super=n_super, total=total, page=page, pages=pages,
                           size=size, sizes=pledges.PAGE_SIZES, f=f, pc=pc, pv=pv, sc=sc, boxes=boxes,
                           can_mark=can_mark_super(), max_votes=pledges.max_votes(), ours=pledges.our_candidate(),
                           label=pledges.label, sort_key=sort_key, sort_dir=sort_dir,
                           query_args={k: v for k, v in request.args.items() if k != "page"},
                           first=(page - 1) * size)


@bp.post("/super/vote")
@super_marker_required
def vote():
    """Mark (1) or unmark (0) a voter as a candidate's super voter: they vote as that candidate decides."""
    from .voters import get_voter_or_404

    nid = request.form.get("national_id", "")
    get_voter_or_404(nid)
    db = get_db()
    cid = request.form.get("candidate_id", type=int)
    if cid is None or not db.execute("SELECT 1 FROM candidates WHERE id = ?", (cid,)).fetchone():
        abort(400)
    wanted = request.form.get("value", "")
    if wanted not in ("0", "1"):
        abort(400)
    was = db.execute("SELECT 1 FROM super_votes WHERE national_id = ? AND candidate_id = ?", (nid, cid)).fetchone() is not None
    limit = pledges.max_votes()
    if wanted == "1" and not was and limit:
        n = db.execute("SELECT COUNT(*) FROM super_votes WHERE national_id = ?", (nid,)).fetchone()[0]
        if n >= limit:
            msg = f"A voter can vote for {limit} candidate{'s' if limit != 1 else ''}. Unmark one first."
            if request.headers.get("X-Requested-With") == "fetch":
                return {"ok": False, "error": msg, "on": False}, 409
            flash(msg, "error")
            return _back(url_for("superpledge.sheet"))
    if wanted == "1" and not was:
        db.execute("INSERT INTO super_votes (national_id, candidate_id, at, by_user) VALUES (?, ?, ?, ?)",
                   (nid, cid, now_iso(), g.user["id"]))
    elif wanted == "0" and was:
        db.execute("DELETE FROM super_votes WHERE national_id = ? AND candidate_id = ?", (nid, cid))
    if (wanted == "1") != was:
        audit.log("super_vote", target=nid, detail={"candidate": cid, "on": wanted == "1"}, commit=False)
        db.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "on": wanted == "1"}
    return _back(url_for("superpledge.sheet"))


# --- scenarios ----------------------------------------------------------------------------------

def read_options(args, cands):
    ids = {c["id"] for c in cands}
    bullet = [int(x) for x in args.getlist("b") if x.isdigit() and int(x) in ids]
    mw = args.get("mw", "0")
    und = args.get("und", "none")

    def pct(key, default):
        value = args.get(key, "")
        return min(100, max(0, int(value))) if value.isdigit() else default

    return {"sv": args.get("sv", "decision") if args.get("sv") in ("decision", "pledge") else "decision",
            "b": bullet, "mw": int(mw) if mw in ("0", "50", "100") else 0,
            "und": und if und in ("none", "split", "ours") else "none",
            "uo": pct("uo", 50), "to": pct("to", 100) or 100, "nsx": args.get("nsx", "1") != "0"}


def _data(where, params):
    """Everything a projection needs, read once."""
    db = get_db()
    sub = f"SELECT national_id FROM voters{where}"
    voters = [r[0] for r in db.execute(sub, params)]
    yes, maybe = {}, {}
    for nid, cid, value in db.execute(f"SELECT national_id, candidate_id, value FROM pledges WHERE value IN ('yes', 'maybe')"
                                      f" AND national_id IN ({sub})", params):
        (yes if value == "yes" else maybe).setdefault(nid, []).append(cid)
    decided = {}
    for nid, cid in db.execute(f"SELECT national_id, candidate_id FROM super_votes WHERE national_id IN ({sub})", params):
        decided.setdefault(nid, []).append(cid)
    supers = set(decided)
    noshow = {r[0] for r in db.execute(f"SELECT national_id FROM pledge_noshow WHERE national_id IN ({sub})", params)}
    return {"voters": voters, "yes": yes, "maybe": maybe, "decided": decided, "supers": supers, "noshow": noshow}


def project(data, opts, cands, ours, limit, n_seats):
    order = {c["id"]: i for i, c in enumerate(cands)}  # ballot order breaks ties
    ids = set(order)
    rank = lambda cs: sorted((c for c in cs if c in ids), key=order.get)  # noqa: E731
    cap = limit or len(cands)
    votes = {c["id"]: 0.0 for c in cands}
    undecided = voting = from_super = 0
    bullet = set(opts["b"])
    for nid in data["voters"]:
        if opts["nsx"] and nid in data["noshow"]:
            continue
        voting += 1
        if opts["sv"] == "decision" and nid in data["supers"] and data["decided"].get(nid):
            for c in rank(data["decided"][nid])[:cap]:
                votes[c] += 1
            from_super += 1
            continue
        yes = rank(data["yes"].get(nid, []))[:cap]
        single = [c for c in yes if c in bullet]
        if single:  # a supporter of a "one vote only" candidate gives just that vote
            votes[single[0]] += 1
            continue
        for c in yes:
            votes[c] += 1
        if opts["mw"]:
            for c in [c for c in rank(data["maybe"].get(nid, [])) if c not in yes][:cap - len(yes)]:
                votes[c] += opts["mw"] / 100
        if not yes and not data["maybe"].get(nid):
            undecided += 1
    if undecided and opts["und"] != "none":
        pool = {c: v for c, v in votes.items() if not (opts["und"] == "ours" and c == ours)}
        total = sum(pool.values())
        left = cap
        if opts["und"] == "ours" and ours in votes:
            votes[ours] += undecided * opts["uo"] / 100
            left = cap - opts["uo"] / 100
        if total:
            for c, v in pool.items():
                votes[c] += undecided * min(1, left * v / total)
    scale = opts["to"] / 100
    result = sorted(({"c": c, "votes": round(votes[c["id"]] * scale)} for c in cands),
                     key=lambda r: (-r["votes"], order[r["c"]["id"]]))
    for i, r in enumerate(result):
        r["place"] = i + 1
        r["wins"] = i < n_seats
    top = max((r["votes"] for r in result), default=0)
    mine = next((r for r in result if r["c"]["id"] == ours), None)
    verdict = None
    if mine and len(result) > n_seats:
        cut_in = result[n_seats - 1]["votes"]   # the last winning place
        first_out = result[n_seats]["votes"]    # the first losing place
        if mine["wins"]:
            verdict = {"wins": True, "margin": mine["votes"] - first_out, "vs": result[n_seats]["c"]["name"]}
        else:
            verdict = {"wins": False, "margin": cut_in - mine["votes"] + 1, "vs": result[n_seats - 1]["c"]["name"]}
    return {"result": result, "top": top, "mine": mine, "verdict": verdict, "voting": round(voting * scale),
            "undecided": undecided, "from_super": from_super}


def presets(cands, ours):
    by_no = {c["ballot_no"]: c for c in cands if c["ballot_no"]}
    first = by_no.get(1) or (cands[0] if cands else None)
    rivals = [c["id"] for c in cands if c["id"] != ours]
    out = [
        ("now", "Pledges as they are", "Yes pledges only; super voters vote as they pledged too.", {"sv": "pledge"}),
        ("s1", "1 · Super voters' decision", "Super voters vote for the candidates they are marked for on the super pledge sheet.",
         {"sv": "decision"}),
    ]
    if first and first["id"] != ours:
        out.append(("s2", f"2 · {pledges.label(first)}'s voters cast only one vote",
                    f"Voters with a Yes for {first['name']} give {first['name']} their only vote; their second vote is lost.",
                    {"sv": "decision", "b": [first["id"]]}))
    if ours:
        out.append(("s3", "3 · Our voters cast only one vote",
                    "Our supporters vote only for us, so rivals lose the second votes they would have had.",
                    {"sv": "decision", "b": [ours]}))
    out.append(("worst", "Worst case", "Every rival's supporters cast only one vote; Maybe counts for nothing.",
                {"sv": "decision", "b": rivals}))
    out.append(("best", "Best case", "Maybe counts fully; undecided voters split like the pledged ones.",
                {"sv": "decision", "mw": 100, "und": "split"}))
    return out


def _opts_args(opts):
    """Query args for a set of options (only what differs from the defaults)."""
    out = {}
    if opts.get("sv", "decision") != "decision":
        out["sv"] = opts["sv"]
    if opts.get("b"):
        out["b"] = list(opts["b"])
    if opts.get("mw"):
        out["mw"] = opts["mw"]
    if opts.get("und", "none") != "none":
        out["und"] = opts["und"]
        if opts["und"] == "ours":
            out["uo"] = opts.get("uo", 50)
    if opts.get("to", 100) != 100:
        out["to"] = opts["to"]
    if not opts.get("nsx", True):
        out["nsx"] = "0"
    return out


@bp.route("/super/scenarios")
@super_viewer_required
def scenarios():
    from .voters import build_where, read_filters

    db = get_db()
    cands = _by_ballot(pledges.candidates())
    f = read_filters(request.args)
    where, params = build_where(f)
    where = f"{where} AND deceased = 0" if where else " WHERE deceased = 0"
    ours = pledges.our_candidate()
    limit, n_seats = pledges.max_votes(), seats()
    data = _data(where, params)
    base = {"sv": "decision", "b": [], "mw": 0, "und": "none", "uo": 50, "to": 100, "nsx": True}
    custom = read_options(request.args, cands)
    compare = []
    for key, title, about, o in presets(cands, ours):
        opts = dict(base, **o)
        compare.append({"key": key, "title": title, "about": about, "args": _opts_args(opts),
                        "p": project(data, opts, cands, ours, limit, n_seats)})
    active = next((c["key"] for c in compare if c["args"] == _opts_args(custom)), "")
    w, p = pledges._scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template("super/scenarios.html", cands=cands, ours=ours, label=pledges.label, limit=limit,
                           n_seats=n_seats, opts=custom, p=project(data, custom, cands, ours, limit, n_seats),
                           compare=compare, active=active, boxes=boxes, f=f, loc=request.args.get("loc", ""),
                           supers=len(data["supers"]))
