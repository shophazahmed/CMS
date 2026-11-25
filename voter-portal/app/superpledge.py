"""Super pledge voters and voting scenarios.

Super pledge voters vote the way a candidate decides. The super pledge sheet lists every voter, like the
pledge sheet, with a cell per candidate: marking it makes the voter that candidate's super voter (up to the
vote limit, e.g. 2 candidates). The scenarios page
projects the result (top seats win) from the pledges, the super voters' decisions and a few what-ifs:
some candidates' supporters cast only one vote, how Maybe and undecided voters go, and turnout.
Access is given by the owner, like the pledge sheet, and needs pledge sheet access too.
"""
import csv
import io
from datetime import datetime

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from . import audit, campaign, pledges
from .auth import can_mark_super, is_owner, super_marker_required, super_viewer_required
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


def _sheet_filters():
    """The pledge sheet filters plus ?sc= (super voters of one candidate, of any, or none)."""
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
    return f, where, params, cands, pc, pv, sc


@bp.route("/super/export.<fmt>")
@super_viewer_required
def export(fmt):
    """The super pledge sheet as CSV or Excel, with the same filters and order. Anyone with super pledge
    access (logged)."""
    from openpyxl.styles import Font, PatternFill

    from .voters import _safe_cell

    if fmt not in ("csv", "xlsx"):
        abort(404)
    f, where, params, cands, pc, pv, sc = _sheet_filters()
    rows = get_db().execute(f"SELECT * FROM voters{where} ORDER BY {pledges.sheet_order()[2]}", params).fetchall()
    marks = _super_map([r["national_id"] for r in rows])
    listno = pledges.list_numbers([r["national_id"] for r in rows])
    connected = pledges.connected_by([r["national_id"] for r in rows], "super_votes")
    agent = pledges.agents_of([r["national_id"] for r in rows])
    headers = (["#", "ID", "Full name", "Sex", "Age", "House name", "Party", "Phone", "Box", "List no.", "Agent",
                "Connected by"]
               + [pledges.label(c) for c in cands] + ["Super for"])
    table = []
    for n, r in enumerate(rows, 1):
        mine = marks.get(r["national_id"], set())
        table.append([n, r["national_id"], _safe_cell(r["full_name"]), r["sex"], r["age"], _safe_cell(r["house_name"]),
                      _safe_cell(r["party"]), r["phone"], _safe_cell(r["location"]), listno.get(r["national_id"], ""),
                      agent.get(r["national_id"], ""), connected.get(r["national_id"], "")]
                     + ["super" if c["id"] in mine else "" for c in cands]
                     + [", ".join(c["name"] for c in cands if c["id"] in mine)])
    audit.log("super_export", target=fmt, detail={"rows": len(table)})
    name = f"super-pledge-{datetime.now().strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(table)
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Super pledge"
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, c in enumerate(cands):
        cell = ws.cell(row=1, column=13 + i)
        cell.fill = PatternFill("solid", fgColor=c["color"].lstrip("#"))
        cell.font = Font(bold=True, color="FFFFFF")
    for row in table:
        ws.append(row)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), headers={"Content-Disposition": f'attachment; filename="{name}"'},
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/super")
@super_viewer_required
def sheet():
    """Every voter, like the pledge sheet; a cell per candidate marks the voter as that candidate's super voter."""
    f, where, params, cands, pc, pv, sc = _sheet_filters()
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
                           label=pledges.label, sort_key=sort_key, sort_dir=sort_dir, owner=is_owner(),
                           listno=pledges.list_numbers([r["national_id"] for r in rows]),
                           connected=pledges.connected_by([r["national_id"] for r in rows], "super_votes"),
                           agents=pledges.agents_of([r["national_id"] for r in rows]),
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

DEFAULTS = {"sv": "decision", "b": [], "sb": [], "mw": 0, "und": "none", "uo": 50, "to": 100, "svt": 100, "nsr": 0,
            "st": {}, "sbt": {}, "sha": None, "shb": None, "shpa": 0, "shpb": 0, "sec": "none", "secp": 100, "swf": None, "swt": None, "swp": 0, "adj": {}}


def read_options(args, cands):
    ids = {c["id"] for c in cands}

    def pct(key, default, hi=100):
        value = args.get(key, "")
        return min(hi, max(0, int(value))) if value.isdigit() else default

    def cand(key):
        value = args.get(key, "")
        return int(value) if value.isdigit() and int(value) in ids else None

    def signed(value):
        value = (value or "").strip()
        neg = value.startswith("-")
        value = value[1:] if neg else value
        return (-int(value) if neg else int(value)) if value.isdigit() and int(value) <= 100000 else 0

    mw = args.get("mw", "0")
    und = args.get("und", "none")
    sec = args.get("sec", "none")
    nsr = pct("nsr", 0)
    if args.get("nsx") == "0":  # the earlier "count no-shows as voting" box
        nsr = 100
    return {
        "sv": args.get("sv") if args.get("sv") in ("decision", "pledge") else "decision",
        "b": [int(x) for x in args.getlist("b") if x.isdigit() and int(x) in ids],
        # these candidates' super voters vote only for that candidate (their other votes are lost)
        "sb": [int(x) for x in args.getlist("sb") if x.isdigit() and int(x) in ids],
        "mw": int(mw) if mw in ("0", "25", "50", "75", "100") else 0,
        "und": und if und in ("none", "split", "ours", "even") else "none",
        "uo": pct("uo", 50), "to": pct("to", 100) or 100, "svt": pct("svt", 100), "nsr": nsr,
        # each candidate's supporters: what share of them turn out to vote
        "st": {c["id"]: pct(f"st{c['id']}", 100) for c in cands if args.get(f"st{c['id']}", "") not in ("", "100")},
        # each candidate's solid base (Yes for them only): what share turn out
        "sbt": {c["id"]: pct(f"sbt{c['id']}", 100) for c in cands if args.get(f"sbt{c['id']}", "") not in ("", "100")},
        # shared supporters of A and B: what share vote only for A, only for B (the rest vote for both)
        "sha": cand("sha"), "shb": cand("shb"), "shpa": pct("shpa", 0), "shpb": pct("shpb", 0),
        # voters with a single Yes: where their second vote goes
        "sec": sec if sec in ("none", "split") or (sec.isdigit() and int(sec) in ids) else "none",
        "secp": pct("secp", 100),
        # a swing: this share of one candidate's supporters vote for another instead
        "swf": cand("swf"), "swt": cand("swt"), "swp": pct("swp", 0),
        # extra votes (or fewer) per candidate, e.g. voters registered elsewhere
        "adj": {c["id"]: signed(args.get(f"adj{c['id']}")) for c in cands if signed(args.get(f"adj{c['id']}"))},
    }


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


def groups(data, cands):
    """Solid base (Yes for one candidate only) and shared supporters (Yes for both of a pair) among the voters
    in the scenario, no-shows left out, like the distribution page."""
    ids = [c["id"] for c in cands]
    solid = {c: 0 for c in ids}
    shared = {}
    for nid, yes in data["yes"].items():
        if nid in data["noshow"]:
            continue
        chosen = sorted({c for c in yes if c in ids}, key=ids.index)
        if len(chosen) == 1:
            solid[chosen[0]] += 1
        for i, a in enumerate(chosen):
            for b in chosen[i + 1:]:
                shared[(a, b)] = shared.get((a, b), 0) + 1
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
    return {"solid": solid, "shared": [(a, b, shared.get((a, b), 0)) for a, b in pairs]}


def project(data, opts, cands, ours, limit, n_seats):
    opts = dict(DEFAULTS, **opts)
    order = {c["id"]: i for i, c in enumerate(cands)}  # ballot order breaks ties
    ids = set(order)
    rank = lambda cs: sorted((c for c in cs if c in ids), key=order.get)  # noqa: E731
    cap = limit or len(cands)
    votes = {c["id"]: 0.0 for c in cands}
    undecided = voting = from_super = 0.0
    bullet = set(opts["b"])
    super_bullet = set(opts["sb"])
    single_yes = {}  # candidate -> weight of voters whose only vote is for them (and could give a second)
    swf, swt, swp = opts["swf"], opts["swt"], opts["swp"] / 100
    sha, shb = opts["sha"], opts["shb"]
    if sha == shb:
        sha = shb = None
    shpa, shpb = opts["shpa"] / 100, opts["shpb"] / 100
    for nid in data["voters"]:
        w = 1.0
        if nid in data["noshow"]:
            w = opts["nsr"] / 100
            if not w:
                continue
        one = [c for c in rank(data["decided"].get(nid, [])) if c in super_bullet]
        if one:  # a super voter of a "one vote only" candidate: that vote and nothing else
            w *= opts["svt"] / 100
            votes[one[0]] += w
            from_super += w
            voting += w
            continue
        if opts["sv"] == "decision" and data["decided"].get(nid):
            w *= opts["svt"] / 100
            for c in rank(data["decided"][nid])[:cap]:
                votes[c] += w
            from_super += w
            voting += w
            continue
        yes = rank(data["yes"].get(nid, []))[:cap]
        if len(yes) == 1 and yes[0] in opts["sbt"]:   # a solid base voter
            w *= opts["sbt"][yes[0]] / 100
        elif yes and opts["st"]:
            w *= sum(opts["st"].get(c, 100) for c in yes) / len(yes) / 100
        if not w:
            continue
        voting += w
        single = [c for c in yes if c in bullet]
        if single:  # a supporter of a "one vote only" candidate gives just that vote
            yes = single[:1]
        else:
            if opts["mw"]:
                for c in [c for c in rank(data["maybe"].get(nid, [])) if c not in yes][:cap - len(yes)]:
                    votes[c] += w * opts["mw"] / 100
            if len(yes) == 1 and cap > 1 and opts["sec"] != "none":
                single_yes[yes[0]] = single_yes.get(yes[0], 0) + w
        keep = {}
        if sha is not None and shb is not None and sha in yes and shb in yes and not single:
            # shared supporters: some vote only for A (B loses them), some only for B
            keep = {sha: max(0.0, 1 - shpb), shb: max(0.0, 1 - shpa)}
        for c in yes:
            k = w * keep.get(c, 1.0)
            if c == swf and swt is not None and swt not in yes and swp:
                votes[c] += k * (1 - swp)
                votes[swt] += k * swp
            else:
                votes[c] += k
        if not yes and not data["maybe"].get(nid):
            undecided += w
    base = dict(votes)
    if single_yes:  # their empty second vote
        share = opts["secp"] / 100
        for c, wt in single_yes.items():
            if opts["sec"] == "split":
                pool = {k: v for k, v in base.items() if k != c}
                total = sum(pool.values())
                for k, v in pool.items():
                    votes[k] += wt * share * (v / total if total else 1 / len(pool))
            elif int(opts["sec"]) != c:
                votes[int(opts["sec"])] += wt * share
    if undecided and opts["und"] != "none":
        if opts["und"] == "even":
            for c in votes:
                votes[c] += undecided * min(1, cap / len(votes))
        else:
            pool = {c: v for c, v in base.items() if not (opts["und"] == "ours" and c == ours)}
            total = sum(pool.values())
            left = cap
            if opts["und"] == "ours" and ours in votes:
                votes[ours] += undecided * opts["uo"] / 100
                left = cap - opts["uo"] / 100
            if total:
                for c, v in pool.items():
                    votes[c] += undecided * min(1, left * v / total)
    scale = opts["to"] / 100
    result = sorted(({"c": c, "votes": max(0, round(votes[c["id"]] * scale) + opts["adj"].get(c["id"], 0))} for c in cands),
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
            "undecided": round(undecided), "from_super": round(from_super)}


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
        out.append(("s3", "3 · Our super voters cast only one vote",
                    "Our super voters vote only for us; the second vote they would have given another candidate is"
                    " taken away. Everyone else votes as pledged.",
                    {"sv": "pledge", "sb": [ours]}))
    out.append(("worst", "Worst case", "Every rival's supporters cast only one vote; Maybe counts for nothing.",
                {"sv": "decision", "b": rivals}))
    out.append(("best", "Best case", "Maybe counts fully; undecided voters split like the pledged ones.",
                {"sv": "decision", "mw": 100, "und": "split"}))
    return out


def _opts_args(opts):
    """Query args for a set of options (only what differs from the defaults)."""
    o = dict(DEFAULTS, **opts)
    out = {}
    if o["sv"] != "decision":
        out["sv"] = o["sv"]
    if o["b"]:
        out["b"] = list(o["b"])
    if o["sb"]:
        out["sb"] = list(o["sb"])
    for key in ("mw", "nsr"):
        if o[key]:
            out[key] = o[key]
    if o["und"] != "none":
        out["und"] = o["und"]
        if o["und"] == "ours":
            out["uo"] = o["uo"]
    for key in ("to", "svt"):
        if o[key] != 100:
            out[key] = o[key]
    if o["sec"] != "none":
        out["sec"] = o["sec"]
        if o["secp"] != 100:
            out["secp"] = o["secp"]
    if o["swp"] and o["swf"] is not None and o["swt"] is not None and o["swf"] != o["swt"]:
        out["swf"], out["swt"], out["swp"] = o["swf"], o["swt"], o["swp"]
    for cid, v in o["st"].items():
        out[f"st{cid}"] = v
    for cid, v in o["sbt"].items():
        out[f"sbt{cid}"] = v
    if o["sha"] is not None and o["shb"] is not None and o["sha"] != o["shb"] and (o["shpa"] or o["shpb"]):
        out["sha"], out["shb"] = o["sha"], o["shb"]
        if o["shpa"]:
            out["shpa"] = o["shpa"]
        if o["shpb"]:
            out["shpb"] = o["shpb"]
    for cid, v in o["adj"].items():
        out[f"adj{cid}"] = v
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
    pp = request.args.get("pp", "")
    cond, extra = pledges.pattern_condition(pp, cands)
    if cond:
        where, params = f"{where} AND {cond}", list(params) + extra
    f["pp"] = pp if cond else ""
    ours = pledges.our_candidate()
    limit, n_seats = pledges.max_votes(), seats()
    data = _data(where, params)
    custom = read_options(request.args, cands)
    compare = []
    for key, title, about, o in presets(cands, ours):
        opts = dict(DEFAULTS, **o)
        compare.append({"key": key, "title": title, "about": about, "args": _opts_args(opts),
                        "p": project(data, opts, cands, ours, limit, n_seats)})
    active = next((c["key"] for c in compare if c["args"] == _opts_args(custom)), "")
    w, p = pledges._scoped_where(" WHERE location != ''")
    boxes = [r[0] for r in db.execute(f"SELECT DISTINCT location FROM voters{w} ORDER BY location COLLATE NOCASE", p)]
    return render_template("super/scenarios.html", cands=cands, ours=ours, label=pledges.label, limit=limit,
                           n_seats=n_seats, opts=custom, p=project(data, custom, cands, ours, limit, n_seats),
                           compare=compare, active=active, boxes=boxes, f=f, loc=request.args.get("loc", ""),
                           supers=len(data["supers"]), groups=groups(data, cands),
                           names={c["id"]: c["name"] for c in cands})
