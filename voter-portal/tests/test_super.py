import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_confirmed import OWNER_PW, add_owner  # noqa: E402
from tests.test_pledges import mark, setup  # noqa: E402


def vote(c, token, nid, cid, value):
    return c.post("/super/vote", data={"csrf_token": token, "national_id": nid, "candidate_id": cid, "value": value},
                  headers={"X-Requested-With": "fetch"})


def owner(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token, ids = setup(o, ("Rayaan", "Sofhaz", "Jawid", "Thalaal"))
    for n, name in enumerate(("Rayaan", "Sofhaz", "Jawid", "Thalaal"), 1):  # ballot numbers 1..4
        cid = ids[name]
        o.post("/pledges/candidates", data={"csrf_token": token, "action": "rename", "id": cid, "name": name,
                                            "color": "#2a78d6", "ballot_no": str(n)})
    o.post("/pledges/candidates", data={"csrf_token": token, "action": "ours", "ours": ids["Sofhaz"]})
    return o, token, ids


def test_super_pledge_sheet(app):
    o, token, ids = owner(app)
    page = o.get("/super").get_data(as_text=True)
    assert "#2 Sofhaz" in page and "Aminath Alpha" in page and "Moosa Beta" in page     # every voter is listed
    assert "★" not in o.get("/pledges").get_data(as_text=True).split("<table")[1]      # its own page, not a column
    assert vote(o, token, "A200001", ids["Sofhaz"], "1").json == {"ok": True, "on": True}
    assert vote(o, token, "A200001", ids["Jawid"], "1").status_code == 200
    assert vote(o, token, "A200001", ids["Rayaan"], "1").status_code == 409            # only 2 candidates
    assert vote(o, token, "A999999", ids["Rayaan"], "1").status_code == 404
    only = o.get(f"/super?sc={ids['Sofhaz']}").get_data(as_text=True)
    assert "Aminath Alpha" in only and "Moosa Beta" not in only
    assert "Moosa Beta" in o.get("/super?sc=none").get_data(as_text=True)
    assert "Aminath Alpha" in o.get("/super?sc=any&sort=age&dir=desc").get_data(as_text=True)
    vote(o, token, "A200001", ids["Jawid"], "0")
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM super_votes").fetchone()[0] == 1


def test_access_like_the_pledge_sheet(app):
    o, token, ids = owner(app)
    viewer = connect(app.config["DATABASE"]).execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    v = app.test_client()
    login(v, "viewer", USER_PW)
    assert v.get("/super").status_code == 403 and v.get("/super/scenarios").status_code == 403
    assert "Super pledge &amp; scenarios" in o.get("/admin/users").get_data(as_text=True)
    o.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": "set_super_access", "level": "view"})
    assert v.get("/super").status_code == 403                    # needs pledge sheet access as well
    o.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": "set_pledge_access", "level": "view"})
    assert v.get("/super").status_code == 200 and v.get("/super/scenarios").status_code == 200
    assert vote(v, csrf(v, "/super"), "A200001", ids["Sofhaz"], "1").status_code == 403    # view only
    o.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": "set_super_access", "level": "mark"})
    assert vote(v, csrf(v, "/super"), "A200001", ids["Sofhaz"], "1").status_code == 200
    # other admins can't hand it out
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.post(f"/admin/users/{viewer}", data={"csrf_token": csrf(b, "/"), "action": "set_super_access",
                                                  "level": "view"}).status_code == 403
    assert b.get("/super").status_code == 403


def test_undecided_and_leaning(app):
    o, token, ids = owner(app)
    mark(o, token, "A200001", ids["Sofhaz"], "yes")
    mark(o, token, "A200002", ids["Rayaan"], "maybe")
    page = " ".join(o.get("/pledges").get_data(as_text=True).split())
    assert "Leaning</span><strong>1</strong>" in page and "Undecided</span><strong>1</strong>" in page
    lean = o.get("/pledges?pv=leaning").get_data(as_text=True)
    assert "Moosa Beta" in lean and "Aminath Alpha</span>" not in lean
    und = o.get("/pledges?pv=undecided").get_data(as_text=True)
    assert "HYPERLINK" in und and "Moosa Beta</span>" not in und
    dash = " ".join(o.get("/dashboard").get_data(as_text=True).split())
    assert "Undecided</span><strong>1</strong>" in dash


def test_scenarios(app):
    from app import superpledge

    o, token, ids = owner(app)
    r, s, j = ids["Rayaan"], ids["Sofhaz"], ids["Jawid"]
    mark(o, token, "A200001", r, "yes")
    mark(o, token, "A200001", j, "yes")            # Rayaan + Jawid
    mark(o, token, "A200002", r, "yes")
    mark(o, token, "A200002", s, "yes")            # Rayaan + Sofhaz
    vote(o, token, "A200003", s, "1")
    vote(o, token, "A200003", j, "1")              # super voter: Sofhaz + Jawid
    with app.test_request_context():
        from flask import g
        g.user = connect(app.config["DATABASE"]).execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        cands = superpledge._by_ballot(__import__("app.pledges", fromlist=["x"]).candidates())
        data = superpledge._data(" WHERE deceased = 0", [])
        base = {"sv": "decision", "b": [], "mw": 0, "und": "none", "uo": 50, "to": 100, "nsx": True}
        votes = lambda o: {x["c"]["name"]: x["votes"] for x in superpledge.project(data, dict(base, **o), cands, s, 2, 2)["result"]}  # noqa: E731
        assert votes({}) == {"Rayaan": 2, "Sofhaz": 2, "Jawid": 2, "Thalaal": 0}
        assert votes({"sv": "pledge"})["Sofhaz"] == 1                     # the super voter pledged nothing
        assert votes({"b": [r]}) == {"Rayaan": 2, "Sofhaz": 1, "Jawid": 1, "Thalaal": 0}
        p = superpledge.project(data, dict(base, b=[r]), cands, s, 2, 2)
        assert p["verdict"] == {"wins": True, "margin": 0, "vs": "Jawid"}  # tie with Jawid, ballot order first
        assert votes({"to": 50})["Rayaan"] == 1
    page = o.get("/super/scenarios").get_data(as_text=True)
    assert "Super voters&#39; decision" in page and "#1 Rayaan&#39;s voters cast only one vote" in page
    assert "Our super voters cast only one vote" in page and "Projected result" in page
    assert o.get(f"/super/scenarios?b={r}&mw=50&und=split&to=80").status_code == 200
    assert o.get("/super/scenarios?und=ours&uo=70&loc=nowhere").status_code == 200


def test_super_export(app):
    import csv
    import io

    from openpyxl import load_workbook

    o, token, ids = owner(app)
    vote(o, token, "A200001", ids["Sofhaz"], "1")
    vote(o, token, "A200001", ids["Jawid"], "1")
    assert "/super/export.xlsx" in o.get("/super").get_data(as_text=True)
    r = o.get(f"/super/export.csv?sc={ids['Sofhaz']}")
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿"))))
    assert rows[0][9:] == ["#1 Rayaan", "#2 Sofhaz", "#3 Jawid", "#4 Thalaal", "Super for"]
    assert len(rows) == 2 and rows[1][1] == "A200001" and rows[1][9:] == ["", "super", "super", "", "Sofhaz, Jawid"]
    wb = load_workbook(io.BytesIO(o.get("/super/export.xlsx").data))
    assert wb.active.max_row == 4                                                    # every voter
    assert "'=HYPERLINK" in [c.value for c in wb.active["C"]][1:] or any(
        str(c.value).startswith("'") for c in wb.active["C"])                         # formulas defused
    # only the owner downloads
    viewer = connect(app.config["DATABASE"]).execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    for action in ("set_super_access", "set_pledge_access"):
        o.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": action, "level": "mark"})
    v = app.test_client()
    login(v, "viewer", USER_PW)
    assert "/super/export" in v.get("/super").get_data(as_text=True)                 # anyone with super access
    assert v.get("/super/export.csv").status_code == 200
    b = app.test_client()
    login(b, "boss", ADMIN_PW)                                                         # no super access
    assert b.get("/super/export.csv").status_code == 403


def test_more_scenario_options(app):
    from app import superpledge

    o, token, ids = owner(app)
    r, s, j, t = ids["Rayaan"], ids["Sofhaz"], ids["Jawid"], ids["Thalaal"]
    mark(o, token, "A200001", r, "yes")
    mark(o, token, "A200001", j, "yes")            # Rayaan + Jawid
    mark(o, token, "A200002", r, "yes")            # Rayaan only
    vote(o, token, "A200003", s, "1")              # super voter for Sofhaz only
    with app.test_request_context():
        from flask import g
        g.user = connect(app.config["DATABASE"]).execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        cands = superpledge._by_ballot(__import__("app.pledges", fromlist=["x"]).candidates())
        data = superpledge._data(" WHERE deceased = 0", [])
        votes = lambda **o: {x["c"]["name"]: x["votes"] for x in superpledge.project(data, o, cands, s, 2, 2)["result"]}  # noqa: E731
        assert votes() == {"Rayaan": 2, "Sofhaz": 1, "Jawid": 1, "Thalaal": 0}
        assert votes(svt=0)["Sofhaz"] == 0                                   # super voters stay home
        assert votes(st={r: 50})["Rayaan"] == 1                              # half of Rayaan's supporters vote
        assert votes(sec=str(s))["Sofhaz"] == 2                              # the single-Yes voter's second vote
        assert votes(sec=str(t), secp=100)["Thalaal"] == 1
        assert votes(swf=r, swt=t, swp=100) == {"Rayaan": 0, "Sofhaz": 1, "Jawid": 1, "Thalaal": 2}
        assert votes(adj={t: 5, j: -9})["Thalaal"] == 5 and votes(adj={j: -9})["Jawid"] == 0
    page = o.get(f"/super/scenarios?svt=80&nsr=50&st{r}=70&sec=split&secp=60&swf={r}&swt={s}&swp=20&adj{t}=3"
                 "&und=even&mw=25&sex=F&age_min=20").get_data(as_text=True)
    assert "Projected result" in page and 'name="swp" min="0" max="100" value="20"' in page
    assert f'value="{s}" selected' in page


def test_only_our_super_voters_cast_one_vote(app):
    from app import superpledge

    o, token, ids = owner(app)
    r, s, j = ids["Rayaan"], ids["Sofhaz"], ids["Jawid"]
    mark(o, token, "A200001", r, "yes")
    mark(o, token, "A200001", s, "yes")            # our super voter, pledged Rayaan + Sofhaz
    vote(o, token, "A200001", s, "1")
    mark(o, token, "A200002", r, "yes")
    mark(o, token, "A200002", s, "yes")            # an ordinary Rayaan + Sofhaz voter: votes as pledged
    mark(o, token, "A200003", j, "yes")
    with app.test_request_context():
        from flask import g
        g.user = connect(app.config["DATABASE"]).execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        cands = superpledge._by_ballot(__import__("app.pledges", fromlist=["x"]).candidates())
        data = superpledge._data(" WHERE deceased = 0", [])
        preset = dict(next(o for k, t, a, o in superpledge.presets(cands, s) if k == "s3"))
        res = {x["c"]["name"]: x["votes"] for x in superpledge.project(data, preset, cands, s, 2, 2)["result"]}
        assert res == {"Rayaan": 1, "Sofhaz": 2, "Jawid": 1, "Thalaal": 0}   # Rayaan loses only the super voter's vote


def test_solid_base_and_shared_in_scenarios(app):
    from app import superpledge

    o, token, ids = owner(app)
    r, s, j = ids["Rayaan"], ids["Sofhaz"], ids["Jawid"]
    mark(o, token, "A200001", r, "yes")
    mark(o, token, "A200001", s, "yes")            # shared Rayaan & Sofhaz
    mark(o, token, "A200002", s, "yes")            # Sofhaz's solid base
    mark(o, token, "A200003", j, "yes")            # Jawid's solid base
    with app.test_request_context():
        from flask import g
        g.user = connect(app.config["DATABASE"]).execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
        cands = superpledge._by_ballot(__import__("app.pledges", fromlist=["x"]).candidates())
        data = superpledge._data(" WHERE deceased = 0", [])
        gr = superpledge.groups(data, cands)
        assert gr["solid"] == {r: 0, s: 1, j: 1, ids["Thalaal"]: 0}
        assert (r, s, 1) in gr["shared"]
        votes = lambda **o: {x["c"]["name"]: x["votes"] for x in superpledge.project(data, o, cands, s, 2, 2)["result"]}  # noqa: E731
        assert votes(sbt={j: 0})["Jawid"] == 0                              # Jawid's solid base stays home
        assert votes(sha=r, shb=s, shpb=100) == {"Rayaan": 0, "Sofhaz": 2, "Jawid": 1, "Thalaal": 0}
        assert votes(sha=r, shb=s, shpa=100)["Sofhaz"] == 1
    page = o.get(f"/super/scenarios?pp=both:{r}-{s}&sha={r}&shb={s}&shpb=50").get_data(as_text=True)
    assert "Solid base and shared supporters" in page and "shared Rayaan &amp; Sofhaz" in page
    assert f"/pledges?pp=only:{s}" not in page                          # nobody is in Sofhaz's solid base here
    assert f"/pledges?pp=only:{s}" in o.get("/super/scenarios").get_data(as_text=True)
