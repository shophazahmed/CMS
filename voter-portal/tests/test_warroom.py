import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, app, csrf, login  # noqa: E402,F401
from tests.test_pledges import mark as pledge  # noqa: E402
from tests.test_super import owner, vote  # noqa: E402

PW = "field-pass-12345"


def make(app, o, token, name, tier, box=""):
    o.post("/admin/users", data={"csrf_token": token, "username": name, "role": "user", "password": PW})
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE users SET must_change_password = 0 WHERE username = ?", (name,))
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]
    o.post("/war/team", data={"csrf_token": token, "action": "set", "user_id": uid, "tier": tier, "war_box": box})
    c = app.test_client()
    login(c, name, PW)
    return c, uid


def setup(app):
    o, token, ids = owner(app)
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Box A'")
    conn.execute("UPDATE voters SET location = 'Box B' WHERE national_id = 'A200003'")
    conn.execute("UPDATE voters SET phone = '7771234' WHERE national_id = 'A200001'")
    conn.commit()
    s = ids["Sofhaz"]
    pledge(o, token, "A200001", s, "yes")                       # solid base
    pledge(o, token, "A200002", s, "yes")
    pledge(o, token, "A200002", ids["Rayaan"], "yes")           # shared
    vote(o, token, "A200003", s, "1")                           # super voter
    return o, token, ids


def test_field_accounts_see_only_their_page(app):
    o, token, ids = setup(app)
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    assert t1.get("/war/box").status_code == 423                # closed until the owner opens it
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    page = t1.get("/war/box").get_data(as_text=True)
    assert "Box A" in page and "Aminath Alpha" not in page      # no list of everyone
    for path in ("/", "/pledges", "/visits", "/voters/A200001", "/dashboard", "/war", "/war/team"):
        r = t1.get(path)
        assert r.status_code in (302, 403), path
        if r.status_code == 302:
            assert r.headers["Location"].endswith(("/war", "/war/box")), path
    assert t1.get("/war/calls").status_code == 403 and t1.get("/war/field").status_code == 403
    # an exact ID at their own box only; no phone numbers for tier 1
    card = t1.get("/war/lookup?q=A200001").json
    assert card["ok"] and card["voter"]["name"] == "Aminath Alpha" and card["voter"]["phone"] == ""
    assert t1.get("/war/lookup?q=A200003").status_code == 404   # another box
    assert t1.get("/war/lookup?q=Aminath").status_code == 404   # no name search
    # closing the War Room stops them at once
    o.post("/war/team", data={"csrf_token": token, "action": "close"})
    assert t1.get("/war/lookup?q=A200001").status_code == 423


def test_marking_clears_the_call_list(app):
    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    t3, caller = make(app, o, token, "caller", "t3")
    o.post("/war/team", data={"csrf_token": token, "action": "allocate"})
    conn = connect(app.config["DATABASE"])
    prios = dict(conn.execute("SELECT national_id, priority FROM war_calls").fetchall())
    assert prios == {"A200003": 1, "A200001": 2, "A200002": 3}       # super, solid base, shared
    page = t3.get("/war/calls").get_data(as_text=True)
    assert "Aminath Alpha" in page and "tel:" in page
    cursor = t3.get("/war/changes?since=0").json["cursor"]
    tok = csrf(t1, "/war/box")
    r = t1.post("/war/mark", data={"csrf_token": tok, "national_id": "A200001", "value": "1", "key": "k1"},
                headers={"X-Requested-With": "fetch"})
    assert r.json == {"ok": True, "voted": True}
    # the same key again (a resend after a signal drop) counts once
    assert t1.post("/war/mark", data={"csrf_token": tok, "national_id": "A200001", "value": "1", "key": "k1"}).json["repeat"]
    assert conn.execute("SELECT COUNT(*) FROM war_log").fetchone()[0] == 1
    # tier 3 hears about it and the voter leaves the list
    ch = t3.get(f"/war/changes?since={cursor}").json
    assert ch["voted"] == ["A200001"]
    assert "Aminath Alpha" not in t3.get("/war/calls").get_data(as_text=True)
    # tier 1 can't mark at another box
    assert t1.post("/war/mark", data={"csrf_token": tok, "national_id": "A200003", "value": "1"}).status_code == 403
    # call outcomes
    assert t3.post("/war/call", data={"csrf_token": csrf(t3, "/war/calls"), "national_id": "A200002", "outcome": "transport"},
                   headers={"X-Requested-With": "fetch"}).json["label"] == "Needs transport"
    dash = " ".join(o.get("/war").get_data(as_text=True).split())
    assert "Turnout" in dash and "Seats: live projection" in dash
    assert tuple(conn.execute("SELECT status, source FROM war_transport WHERE national_id = 'A200002'").fetchone()) == ("waiting", "call")
    # undo: their own mark, soon after
    assert t1.post("/war/mark", data={"csrf_token": tok, "national_id": "A200001", "value": "0"}).json["voted"] is False
    assert "Aminath Alpha" in t3.get("/war/calls").get_data(as_text=True)


def test_list_numbers_and_tier2(app):
    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    sheet = io.BytesIO(b"ID,No.\nA200001,17\nA200002,18\nZ999999,19\n")
    r = o.post("/war/team", data={"csrf_token": token, "action": "import", "sheet": (sheet, "list.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "numbers saved for 2 voters" in r.get_data(as_text=True)
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    assert t1.get("/war/lookup?q=17").json["voter"]["national_id"] == "A200001"
    assert t1.get("/war/lookup?q=99").status_code == 404
    # tier 2 sees their houses' supporters who haven't voted
    t2, runner = make(app, o, token, "runner", "t2")
    conn = connect(app.config["DATABASE"])
    house = conn.execute("SELECT house_name FROM voters WHERE national_id = 'A200001'").fetchone()[0]
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, '2026-01-01')", (house, runner))
    conn.commit()
    page = t2.get("/war/field").get_data(as_text=True)
    assert "Aminath Alpha" in page and "Moosa Beta" not in page
    assert t2.post("/war/mark", data={"csrf_token": csrf(t2, "/war/field"), "national_id": "A200002", "value": "1"}).status_code == 403
    assert t2.post("/war/mark", data={"csrf_token": csrf(t2, "/war/field"), "national_id": "A200001", "value": "1"}).json["voted"]
    assert "Aminath Alpha" not in t2.get("/war/field").get_data(as_text=True)


def test_only_the_owner_sets_it_up(app):
    o, token, ids = setup(app)
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.get("/war").headers["Location"].endswith("/war/team")          # an admin without a tier: the team page
    team = b.get("/war/team").get_data(as_text=True)
    assert "War Room team" in team and "Only the owner opens or closes" in team and 'value="open"' not in team
    conn = connect(app.config["DATABASE"])
    uid = conn.execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    btok = csrf(b, "/war/team")
    b.post("/war/team", data={"csrf_token": btok, "action": "set", "user_id": uid, "tier": "room"})   # not the dashboard
    assert conn.execute("SELECT war_tier FROM users WHERE id = ?", (uid,)).fetchone()[0] == ""
    b.post("/war/team", data={"csrf_token": btok, "action": "set", "user_id": viewer, "tier": "t2"})  # field roles: yes
    assert conn.execute("SELECT war_tier FROM users WHERE id = ?", (viewer,)).fetchone()[0] == "t2"
    assert b.post("/war/team", data={"csrf_token": btok, "action": "open"}).status_code == 403
    assert b.get("/war/sheets").status_code == 403
    o.post("/war/team", data={"csrf_token": token, "action": "set", "user_id": uid, "tier": "room"})
    assert b.get("/war").status_code == 200
    assert "War Room" in b.get("/").get_data(as_text=True)       # admins keep the whole portal



def test_transport_queue(app):
    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    t2, runner = make(app, o, token, "runner", "t2")
    other, _ = make(app, o, token, "runner2", "t2")
    conn = connect(app.config["DATABASE"])
    house = conn.execute("SELECT house_name FROM voters WHERE national_id = 'A200001'").fetchone()[0]
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, '2026-01-01')", (house, runner))
    conn.commit()
    tok = csrf(t2, "/war/field")
    act = lambda c, nid, a: c.post("/war/transport", data={"csrf_token": csrf(c, "/war/transport"), "national_id": nid,  # noqa: E731
                                                          "action": a}, headers={"X-Requested-With": "fetch"})
    assert act(t2, "A200002", "request").status_code == 403             # not in their houses
    assert act(t2, "A200001", "request").json["ok"]
    assert "Aminath Alpha" in other.get("/war/transport").get_data(as_text=True)   # every runner sees the queue
    assert act(t2, "A200001", "take").json["ok"]
    assert act(other, "A200001", "done").status_code == 409             # someone else's ride
    assert act(other, "A200001", "take").status_code == 409             # already taken
    page = o.get("/war").get_data(as_text=True)
    assert 'data-k="t_onway">1<' in page
    # once they vote they leave the queue
    t2.post("/war/mark", data={"csrf_token": tok, "national_id": "A200001", "value": "1"})
    assert "Aminath Alpha" not in t2.get("/war/transport").get_data(as_text=True)
    assert 'data-k="t_done">1<' in o.get("/war").get_data(as_text=True)
    # tier 1 and tier 3 can't use the queue
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    assert t1.get("/war/transport").status_code in (302, 403)


def test_sheets(app):
    from openpyxl import load_workbook

    o, token, ids = setup(app)
    make(app, o, token, "boxagent", "t1", "Box A")
    caller, _ = make(app, o, token, "caller", "t3")
    o.post("/war/team", data={"csrf_token": token, "action": "allocate"})
    page = o.get("/war/sheets").get_data(as_text=True)
    assert "boxagent" in page and "Confidential: for caller only" in page and "Aminath Alpha" in page
    wb = load_workbook(io.BytesIO(o.get("/war/sheets.xlsx").data))
    assert wb.sheetnames == ["boxagent", "caller"]
    assert wb["caller"].max_row == 2 + 3                                   # header lines + 3 supporters
    assert caller.get("/war/sheets").status_code in (302, 403)            # owner only
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.get("/war/sheets.xlsx").status_code == 403



def test_dashboard_agent_filter(app):
    o, token, ids = setup(app)
    conn = connect(app.config["DATABASE"])
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    conn.execute("INSERT INTO voter_assignments (national_id, user_id, assigned_at) VALUES ('A200001', ?, '2026-01-01')", (viewer,))
    conn.execute("INSERT INTO war_voted (national_id, at, by_user, tier) VALUES ('A200002', '2026-01-01T08:00:00+00:00', 1, 'owner')")
    conn.commit()
    whole = " ".join(o.get("/war").get_data(as_text=True).split())
    assert 'data-k="voted">1<' in whole and 'name="ag"' in whole
    mine = " ".join(o.get(f"/war?ag={viewer}").get_data(as_text=True).split())
    assert "War Room · viewer" in mine and 'data-k="voted">0<' in mine and "</span> of 1 voted" in mine
    assert 'data-k="sup_pending">1<' in mine                              # A200001, the only supporter of theirs
    assert o.get(f"/war/changes?since=0&stats=1&ag={viewer}").json["stats"]["voted"] == 0
    assert o.get("/war?ag=none").status_code == 200



def test_agent_links_and_contact_statuses(app):
    from tests.test_canvass import add_agent

    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    add_agent(app, "ali")                                                  # a canvasser who never signs in
    conn = connect(app.config["DATABASE"])
    ali = conn.execute("SELECT id FROM users WHERE username = 'ali'").fetchone()[0]
    conn.execute("UPDATE users SET can_login = 0 WHERE id = ?", (ali,))
    house = conn.execute("SELECT house_name FROM voters WHERE national_id = 'A200001'").fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, '2026-01-01')", (house, ali))
    conn.commit()
    team = o.get("/war/team").get_data(as_text=True)
    assert "ali" in team and "no sign-in" in team                        # agents are on the team page
    b = app.test_client()                                                  # another admin gives the role and the link
    login(b, "boss", ADMIN_PW)
    btok = csrf(b, "/war/team")
    b.post("/war/team", data={"csrf_token": btok, "action": "set", "user_id": ali, "tier": "t2"})
    b.post("/war/team", data={"csrf_token": btok, "action": "link", "user_id": ali, "days": "7"})
    token_ = conn.execute("SELECT token FROM war_links WHERE user_id = ? AND revoked_at IS NULL", (ali,)).fetchone()[0]
    assert f"/w/{token_}" in b.get("/war/team").get_data(as_text=True)
    # the agent opens the link: their supporters, nothing else
    a = app.test_client()
    r = a.get(f"/w/{token_}")
    assert r.headers["Location"].endswith("/war")
    page = a.get("/war/field").get_data(as_text=True)
    assert "Aminath Alpha" in page and "Not reached yet" in page and "Needs transport" in page
    assert a.get("/pledges").headers["Location"].endswith("/war")
    assert a.get("/war/team").status_code in (302, 403)
    # what the voter said
    atok = csrf(a, "/war/field")
    r = a.post("/war/contact", data={"csrf_token": atok, "national_id": "A200001", "status": "transport"},
               headers={"X-Requested-With": "fetch"})
    assert r.json == {"ok": True, "status": "transport", "label": "Needs transport"}
    assert tuple(conn.execute("SELECT status, source FROM war_transport WHERE national_id = 'A200001'").fetchone()) == ("waiting", "field")
    assert a.post("/war/contact", data={"csrf_token": atok, "national_id": "A200002", "status": "coming"}).status_code == 403
    assert "Needs transport · " in a.get("/war/field?cs=transport").get_data(as_text=True)
    dash = " ".join(o.get("/war").get_data(as_text=True).split())
    assert "Reached before the vote" in dash and 'data-k="c_transport">1<' in dash
    # stopping the link signs them out at once
    b.post("/war/team", data={"csrf_token": btok, "action": "unlink", "user_id": ali})
    assert a.get("/war/field").status_code == 302
    assert a.get(f"/w/{token_}").status_code == 410
