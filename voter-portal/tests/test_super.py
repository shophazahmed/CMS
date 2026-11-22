import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_confirmed import OWNER_PW, add_owner  # noqa: E402
from tests.test_pledges import mark, setup  # noqa: E402


def toggle(c, token, nid, value):
    return c.post("/super/toggle", data={"csrf_token": token, "national_id": nid, "value": value},
                  headers={"X-Requested-With": "fetch"})


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


def test_super_voters_and_their_decision(app):
    o, token, ids = owner(app)
    assert "#2 Sofhaz" in o.get("/super").get_data(as_text=True)
    assert toggle(o, token, "A200001", "1").json == {"ok": True, "on": True}
    assert "★" in o.get("/pledges").get_data(as_text=True)
    assert vote(o, token, "A200001", ids["Sofhaz"], "1").json["on"] is True
    assert vote(o, token, "A200001", ids["Jawid"], "1").status_code == 200
    assert vote(o, token, "A200001", ids["Rayaan"], "1").status_code == 409            # only 2 votes
    assert vote(o, token, "A200002", ids["Rayaan"], "1").status_code == 404            # not a super voter
    page = o.get("/super").get_data(as_text=True)
    assert "Aminath Alpha" in page and "Moosa Beta" not in page
    # taking them off drops the decision
    toggle(o, token, "A200001", "0")
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM super_votes").fetchone()[0] == 0


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
    assert toggle(v, csrf(v, "/super"), "A200001", "1").status_code == 403    # view only
    o.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": "set_super_access", "level": "mark"})
    assert toggle(v, csrf(v, "/super"), "A200001", "1").status_code == 200
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
    toggle(o, token, "A200003", "1")
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
    assert "Our voters cast only one vote" in page and "Projected result" in page
    assert o.get(f"/super/scenarios?b={r}&mw=50&und=split&to=80").status_code == 200
    assert o.get("/super/scenarios?und=ours&uo=70&loc=nowhere").status_code == 200
