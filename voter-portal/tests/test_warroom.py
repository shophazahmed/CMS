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
    assert "Box A" in page and "Voter list" in page and "Aminath Alpha" in page      # their box's list
    assert "HYPERLINK" not in page                               # A200003 is at Box B: not on it
    assert "7771234" not in page                                 # no phone numbers for box agents
    assert "Not yet" in page
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
    assert "✓ Voted" in t1.get("/war/box").get_data(as_text=True)       # the list shows who voted
    import csv as _csv
    rows = list(_csv.reader(io.StringIO(t1.get("/war/box/export.csv").get_data(as_text=True).lstrip("\ufeff"))))
    assert rows[0] == ["No.", "Name", "House", "Age", "Sex", "Voted", "Time"] and len(rows) == 3   # Box A: 2 voters
    assert [r[5] for r in rows[1:] if r[1] == "Aminath Alpha"] == ["Voted"]
    assert len(list(_csv.reader(io.StringIO(t1.get("/war/box/export.csv?show=no").get_data(as_text=True))))) == 2
    assert "HYPERLINK" not in t1.get("/war/box/export.csv?box=Box+B").get_data(as_text=True)     # always their own box
    # photos: on the list and in the Excel; only their own box's voters
    import os
    from PIL import Image
    for nid in ("A200001", "A200003"):
        Image.new("RGB", (200, 260), "gray").save(os.path.join(app.config["PHOTOS_DIR"], f"{nid}.jpg"))
        conn.execute("UPDATE voters SET photo_path = ? WHERE national_id = ?", (f"{nid}.jpg", nid))
    conn.commit()
    assert "/photos/A200001" in t1.get("/war/box").get_data(as_text=True)
    assert t1.get("/photos/A200001").status_code == 200
    assert t1.get("/photos/A200003").status_code == 404                     # Box B: not theirs
    assert o.get("/photos/A200003").status_code == 200                      # the owner: any
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(t1.get("/war/box/export.xlsx").data)).active
    assert ws["A2"].value == "Photo" and len(ws._images) == 1
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


def test_supporters_export(app):
    import csv as _csv

    from openpyxl import load_workbook

    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    t2, runner = make(app, o, token, "runner", "t2")
    conn = connect(app.config["DATABASE"])
    house = conn.execute("SELECT house_name FROM voters WHERE national_id = 'A200001'").fetchone()[0]
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, '2026-01-01')", (house, runner))
    conn.commit()
    t2.post("/war/contact", data={"csrf_token": csrf(t2, "/war/field"), "national_id": "A200001", "status": "coming"})
    page = o.get(f"/war/field?u={runner}").get_data(as_text=True)
    assert "/war/field/export.xlsx" in page
    assert "/war/field/export" in t2.get("/war/field").get_data(as_text=True)       # runners get their own list
    own = t2.get("/war/field/export.csv?u=all").get_data(as_text=True)              # 'all' is ignored for them
    assert own.count("\n") == 3 and "A200001" in own
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    assert t1.get("/war/field/export.csv").status_code in (302, 403)                # other tiers can't
    o.post("/war/team", data={"csrf_token": token, "action": "close"})
    assert t2.get("/war/field/export.csv").status_code == 423                       # not while closed
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    rows = list(_csv.reader(io.StringIO(o.get(f"/war/field/export.csv?u={runner}").get_data(as_text=True).lstrip("﻿"))))
    assert rows[0][:4] == ["#", "Runner", "ID", "Name"] and len(rows) == 3    # two supporters in that house
    mine = {r[2]: r for r in rows[1:]}
    assert mine["A200001"][1] == "runner" and mine["A200001"][9] == "Coming" and mine["A200003"][9] == "Not reached yet"
    assert mine["A200003"][3].startswith("'")                                    # formulas defused
    assert len(list(_csv.reader(io.StringIO(o.get(f"/war/field/export.csv?u={runner}&cs=none").get_data(as_text=True))))) == 2
    wb = load_workbook(io.BytesIO(o.get("/war/field/export.xlsx?u=all").data))
    assert wb.active.max_row == 3


def test_call_list_export(app):
    import csv as _csv

    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    t3, caller = make(app, o, token, "caller", "t3")
    other, _ = make(app, o, token, "caller2", "t3")
    o.post("/war/team", data={"csrf_token": token, "action": "allocate"})
    assert "/war/calls/export.xlsx" in t3.get("/war/calls").get_data(as_text=True)
    mine = list(_csv.reader(io.StringIO(t3.get("/war/calls/export.csv?u=all").get_data(as_text=True).lstrip("﻿"))))
    assert mine[0][1] == "Caller" and {r[1] for r in mine[1:]} == {"caller"}            # only their own
    everyone = list(_csv.reader(io.StringIO(o.get("/war/calls/export.csv?u=all").get_data(as_text=True).lstrip("﻿"))))
    assert len(everyone) == 4 and {r[1] for r in everyone[1:]} == {"caller", "caller2"}
    t2, _ = make(app, o, token, "runner", "t2")
    assert t2.get("/war/calls/export.csv").status_code in (302, 403)


def test_one_agent_several_tiers_and_the_war_room(app):
    from tests.test_canvass import add_agent

    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    both, uid = make(app, o, token, "both", "")
    o.post("/war/team", data={"csrf_token": token, "action": "set", "user_id": uid, "tiers": ["t1", "t2"], "war_box": "Box B"})
    conn = connect(app.config["DATABASE"])
    assert tuple(conn.execute("SELECT war_tier, war_box FROM users WHERE id = ?", (uid,)).fetchone()) == ("t1,t2", "Box B")
    house = conn.execute("SELECT house_name FROM voters WHERE national_id = 'A200001'").fetchone()[0]
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, '2026-01-01')", (house, uid))
    conn.commit()
    box = both.get("/war/box").get_data(as_text=True)
    assert "Box B" in box and "My supporters" in box and "Vote count" in box          # both tiers in the menu
    field = both.get("/war/field").get_data(as_text=True)
    assert "Aminath Alpha" in field                                                  # tier 2: their supporters
    assert both.get("/war/transport").status_code == 200
    assert both.get("/war/calls").status_code == 403                                 # no tier 3
    assert both.get("/war/lookup?q=A200003").json["ok"]                              # tier 1: their box (Box B)
    tok = csrf(both, "/war/box")
    assert both.post("/war/mark", data={"csrf_token": tok, "national_id": "A200003", "value": "1"}).json["voted"]  # box
    assert both.post("/war/mark", data={"csrf_token": tok, "national_id": "A200001", "value": "1"}).json["voted"]  # runner
    assert both.post("/war/mark", data={"csrf_token": tok, "national_id": "A200002", "value": "1"}).status_code == 403
    assert both.get("/pledges").status_code == 302                                    # still a field account
    # the War Room dashboard as well (owner only), for an agent who signs in
    o.post("/war/team", data={"csrf_token": token, "action": "set", "user_id": uid, "tiers": ["room", "t1", "t2"],
                              "war_box": "Box B"})
    assert conn.execute("SELECT war_tier FROM users WHERE id = ?", (uid,)).fetchone()[0] == "room,t1,t2"
    assert "War Room" in both.get("/war").get_data(as_text=True) and both.get("/war").status_code == 200
    assert "Box B" in both.get("/war/box").get_data(as_text=True)                    # opens their own box first
    # an admin who isn't the owner can't give or take it
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    r = b.post("/war/team", data={"csrf_token": csrf(b, "/war/team"), "action": "set", "user_id": uid, "tiers": ["t1"]},
               follow_redirects=True)
    assert "Only the owner" in r.get_data(as_text=True)
    assert conn.execute("SELECT war_tier FROM users WHERE id = ?", (uid,)).fetchone()[0] == "room,t1,t2"
    # an agent without a password can't get the dashboard; their private link opens field pages only
    add_agent(app, "ali")
    ali = conn.execute("SELECT id FROM users WHERE username = 'ali'").fetchone()[0]
    conn.execute("UPDATE users SET can_login = 0 WHERE id = ?", (ali,))
    conn.commit()
    r = o.post("/war/team", data={"csrf_token": token, "action": "set", "user_id": ali, "tiers": ["room", "t2"]},
               follow_redirects=True)
    assert "Allow sign-in" in r.get_data(as_text=True)
    conn.execute("UPDATE users SET war_tier = 'room,t2' WHERE id = ?", (ali,))            # even if it got in somehow
    conn.commit()
    o.post("/war/team", data={"csrf_token": token, "action": "link", "user_id": ali, "days": "7"})
    link = conn.execute("SELECT token FROM war_links WHERE user_id = ? AND revoked_at IS NULL", (ali,)).fetchone()[0]
    a = app.test_client()
    a.get(f"/w/{link}")
    assert a.get("/war/field").status_code == 200
    assert a.get("/war").status_code == 302 and a.get("/war/team").status_code in (302, 403)


def test_team_and_users_pages_forms_are_closed(app):
    """Every form closes before the next one opens: a missing </form> makes one row's Save send the rows below."""
    import re as _re

    from tests.test_canvass import add_agent

    o, token, ids = setup(app)
    add_agent(app, "ali")
    _, uid = make(app, o, token, "runner", "t2")
    o.post("/war/team", data={"csrf_token": token, "action": "link", "user_id": uid, "days": "7"})
    for path in ("/war/team", "/admin/users"):
        page = o.get(path).get_data(as_text=True)
        depth = 0
        for tag in _re.findall(r"<(/?)form\b", page):
            depth += -1 if tag else 1
            assert depth in (0, 1), path                                # never a form inside a form
        assert depth == 0, path
    # making a link from the team page makes a link (it used to re-save the tiers)
    o.post("/war/team", data={"csrf_token": token, "action": "link", "user_id": uid, "days": "3"})
    conn = connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM war_links WHERE user_id = ?", (uid,)).fetchone()[0] == 2


def test_alerts_button_and_live_alerts(app):
    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    page = o.get("/war").get_data(as_text=True)
    assert "data-alert-btn" in page and 'aria-controls="war-alerts"' in page and 'id="war-alerts"' in page
    assert "closes in closed" not in page
    data = o.get("/war/changes?since=0&stats=1").json["stats"]
    assert isinstance(data["alerts"], list) and data["n_alerts"] == len(data["alerts"])
    assert data["left"] == "" or data["left"].startswith(("closes in ", "polls closed"))


def test_voted_list_and_hidden_alerts(app):
    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    page = o.get("/war").get_data(as_text=True)
    assert 'data-alerts hidden' in page and "/war/voted" in page                      # alerts behind the button
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    tok = csrf(t1, "/war/box")
    t1.post("/war/mark", data={"csrf_token": tok, "national_id": "A200001", "value": "1"})
    t1.post("/war/mark", data={"csrf_token": tok, "national_id": "A200002", "value": "1"})
    page = o.get("/war/voted").get_data(as_text=True)
    assert "Aminath Alpha" in page and "Moosa Beta" in page and "boxagent" in page and "2 voted" in page
    assert "Solid base" in page                                                       # our supporter group
    only = o.get("/war/voted?grp=2").get_data(as_text=True)
    assert "Aminath Alpha" in only and "Moosa Beta" not in only
    assert "Aminath" not in o.get("/war/voted?box=Box+B").get_data(as_text=True)
    assert "Moosa Beta" in o.get("/war/voted?q=moosa").get_data(as_text=True)
    rows = o.get("/war/voted/export.csv").get_data(as_text=True).strip().splitlines()
    assert len(rows) == 3 and "Marked by" in rows[0]
    assert t1.get("/war/voted").status_code in (302, 403)                             # not for field accounts
    assert t1.get("/war/voted/export.csv").status_code in (302, 403)


def test_agent_sheet_pdf_and_no_mark_popup(app):
    o, token, ids = setup(app)
    r = o.get("/agents/sheet/export.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:4] == b"%PDF"
    from app.pdfexport import _mupdf
    text = _mupdf().open("pdf", r.data)[0].get_text()
    assert "Agents and their voters" in text and "Aminath Alpha" in text
    assert "/agents/sheet/export.pdf" in o.get("/agents/sheet").get_data(as_text=True)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    _, runner = make(app, o, token, "runner", "t2")
    assert "as voted?" not in o.get(f"/war/field?u={runner}").get_data(as_text=True)


def test_box_agent_on_a_link_sees_the_box_photos(app):
    from tests.test_canvass import add_agent

    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    add_agent(app, "boxali")
    conn = connect(app.config["DATABASE"])
    ali = conn.execute("SELECT id FROM users WHERE username = 'boxali'").fetchone()[0]
    conn.execute("UPDATE users SET can_login = 0 WHERE id = ?", (ali,))
    conn.execute("UPDATE voters SET photo_path = 'p.jpg'")
    conn.commit()
    import os
    with open(os.path.join(app.config["PHOTOS_DIR"], "p.jpg"), "wb") as fh:
        fh.write(b"\xff\xd8\xff\xd9")
    o.post("/war/team", data={"csrf_token": token, "action": "set", "user_id": ali, "tiers": ["t1"], "war_box": "Box A"})
    o.post("/war/team", data={"csrf_token": token, "action": "link", "user_id": ali, "days": "7"})
    link = conn.execute("SELECT token FROM war_links WHERE user_id = ?", (ali,)).fetchone()[0]
    a = app.test_client()
    a.get(f"/w/{link}")
    assert a.get("/photos/A200001").status_code == 200        # at their box, though not in houses they canvass
    assert a.get("/photos/A200003").status_code == 404        # Box B: not theirs


def test_box_list_follows_the_official_list(app):
    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    conn = connect(app.config["DATABASE"])
    # Box A has A200001 and A200002 in the portal; the official list has only A200001 (number 7)
    conn.execute("INSERT INTO war_list (national_id, list_no, box) VALUES ('A200001', 7, 'Box A')")
    conn.commit()
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    page = t1.get("/war/box").get_data(as_text=True)
    assert "Aminath Alpha" in page and "Moosa Beta" not in page            # only numbered voters
    assert t1.get("/war/lookup?q=7").json["ok"]
    assert t1.get("/war/lookup?q=A200002").status_code == 404              # not on the official list
    rows = t1.get("/war/box/export.csv").get_data(as_text=True).strip().splitlines()
    assert len(rows) == 2
    owner_page = o.get("/war/box?box=Box+A").get_data(as_text=True)
    assert "1 voter the portal has at Box A" in owner_page                 # the War Room is told who's left out
    # a box with no official list yet still uses the portal's box
    assert "Box B" in o.get("/war/box?box=Box+B").get_data(as_text=True)
    dash = o.get("/war").get_data(as_text=True)
    assert "Not on an official list" in dash                               # left-out voters counted apart


def test_public_results_page_and_clearing_test_votes(app):
    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    tok = csrf(t1, "/war/box")
    t1.post("/war/mark", data={"csrf_token": tok, "national_id": "A200001", "value": "1"})
    conn = connect(app.config["DATABASE"])
    # only the owner makes the link
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.post("/war/team", data={"csrf_token": csrf(b, "/war/team"), "action": "public_link"}).status_code == 403
    o.post("/war/team", data={"csrf_token": token, "action": "public_link"})
    code = conn.execute("SELECT value FROM settings WHERE key = 'public_board_token'").fetchone()[0]
    anyone = app.test_client()                                                   # not signed in
    page = anyone.get(f"/live/{code}").get_data(as_text=True)
    assert "Turnout by box" in page and "Box A" in page and "Vote count" in page
    for secret in ("A200001", "Aminath", "Moosa", "Solid base", "Super voter", "boxagent", "pledge"):
        assert secret not in page, secret                                       # totals only
    assert anyone.get("/live/wrong-code").status_code == 404
    o.post("/war/team", data={"csrf_token": token, "action": "public_count", "on": "0"})
    assert "Vote count" not in anyone.get(f"/live/{code}").get_data(as_text=True)
    o.post("/war/team", data={"csrf_token": token, "action": "public_stop"})
    assert anyone.get(f"/live/{code}").status_code == 404
    # clearing test votes: owner only, typed confirmation; logged as undos so open pages update
    cursor = t1.get("/war/changes?since=0").json["cursor"]
    r = o.post("/war/team", data={"csrf_token": token, "action": "clear_votes", "confirm": "nope"}, follow_redirects=True)
    assert "Type CLEAR" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM war_voted").fetchone()[0] == 1
    r = o.post("/war/team", data={"csrf_token": token, "action": "clear_votes", "confirm": "clear"}, follow_redirects=True)
    assert "1 voted mark removed" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM war_voted").fetchone()[0] == 0
    assert t1.get(f"/war/changes?since={cursor}").json["undone"] == ["A200001"]


def test_public_vote_count_page_and_removing_a_box(app):
    o, token, ids = setup(app)
    o.post("/war/team", data={"csrf_token": token, "action": "public_link"})
    conn = connect(app.config["DATABASE"])
    code = conn.execute("SELECT value FROM settings WHERE key = 'public_board_token'").fetchone()[0]
    anyone = app.test_client()
    count = anyone.get(f"/live/{code}/count").get_data(as_text=True)
    assert "Vote count" in count and "Turnout by box" not in count and f"/live/{code}" in count
    assert f"/live/{code}/count" in anyone.get(f"/live/{code}").get_data(as_text=True)
    assert f"/live/{code}/count" in o.get("/war/team").get_data(as_text=True)          # the owner can send it
    o.post("/war/team", data={"csrf_token": token, "action": "public_count", "on": "0"})
    assert anyone.get(f"/live/{code}/count").status_code == 404
    # remove an old box (Box B has A200003, a super voter): owner only, typed name
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.post("/war/team", data={"csrf_token": csrf(b, "/war/team"), "action": "remove_box", "box": "Box B",
                                     "confirm": "Box B"}).status_code == 403
    r = o.post("/war/team", data={"csrf_token": token, "action": "remove_box", "box": "Box B", "confirm": "box b"},
               follow_redirects=True)
    assert "Type the box name" in r.get_data(as_text=True)
    assert "Box B" in o.get("/war/team").get_data(as_text=True)
    r = o.post("/war/team", data={"csrf_token": token, "action": "remove_box", "box": "Box B", "confirm": "Box B"},
               follow_redirects=True)
    assert "Removed Box B: 1 voter" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM voters WHERE national_id = 'A200003'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM super_votes WHERE national_id = 'A200003'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0] == 2                 # Box A untouched
    assert "Box B" not in anyone.get(f"/live/{code}").get_data(as_text=True)


def test_match_boxes_to_the_official_lists(app):
    o, token, ids = setup(app)
    conn = connect(app.config["DATABASE"])
    # A200002 is on Box B's official list but its box field still says Box A
    conn.execute("INSERT INTO war_list (national_id, list_no, box) VALUES ('A200003', 1, 'Box B'), ('A200002', 2, 'Box B')")
    conn.commit()
    team = o.get("/war/team").get_data(as_text=True)
    assert "Box field differs" in team and "Match boxes to the official lists (1)" in team
    r = o.post("/war/team", data={"csrf_token": token, "action": "sync_boxes"}, follow_redirects=True)
    assert "official lists for 1 voter" in r.get_data(as_text=True)
    assert conn.execute("SELECT location FROM voters WHERE national_id = 'A200002'").fetchone()[0] == "Box B"
    assert "Match boxes to the official lists (1)" not in o.get("/war/team").get_data(as_text=True)
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.post("/war/team", data={"csrf_token": csrf(b, "/war/team"), "action": "sync_boxes"}).status_code == 403


def test_public_candidate_photos_and_unlisting_a_voter(app):
    import io as _io

    from PIL import Image

    o, token, ids = setup(app)
    cid = ids["Sofhaz"]
    o.post("/pledges/candidates", data={"csrf_token": token, "action": "rename", "id": cid, "name": "Sofhaz",
                                        "color": "#2a78d6", "ballot_no": "2", "full_name": "Sofhaz Ahmed", "place": "Gadhdhoo, Seenuge"})
    img = _io.BytesIO()
    Image.new("RGB", (40, 50), "blue").save(img, "PNG")
    img.seek(0)
    o.post("/pledges/candidates", data={"csrf_token": token, "action": "photo", "id": cid, "photo": (img, "s.png")},
           content_type="multipart/form-data")
    o.post("/war/team", data={"csrf_token": token, "action": "public_link"})
    conn = connect(app.config["DATABASE"])
    code = conn.execute("SELECT value FROM settings WHERE key = 'public_board_token'").fetchone()[0]
    anyone = app.test_client()
    page = anyone.get(f"/live/{code}/count").get_data(as_text=True)
    assert "Sofhaz Ahmed" in page and "Gadhdhoo, Seenuge" in page and f"/live/{code}/candidate/{cid}.jpg" in page
    assert anyone.get(f"/live/{code}/candidate/{cid}.jpg").mimetype == "image/jpeg"
    assert anyone.get(f"/live/wrong/candidate/{cid}.jpg").status_code == 404
    # the War Room takes a wrong match off a box's list
    conn.execute("INSERT INTO war_list (national_id, list_no, box) VALUES ('A200001', 1, 'Box A'), ('A200002', 2, 'Box A')")
    conn.commit()
    assert "Not at this box" in o.get("/war/box?box=Box+A").get_data(as_text=True)
    o.post("/war/box/unlist", data={"csrf_token": token, "national_id": "A200002", "box": "Box A", "to": "Box B"})
    assert [r[0] for r in conn.execute("SELECT national_id FROM war_list")] == ["A200001"]
    assert conn.execute("SELECT location FROM voters WHERE national_id = 'A200002'").fetchone()[0] == "Box B"
    # a box with no official list: the box field moves (to no box)
    o.post("/war/box/unlist", data={"csrf_token": token, "national_id": "A200003", "box": "Box B", "to": ""})
    assert conn.execute("SELECT location FROM voters WHERE national_id = 'A200003'").fetchone()[0] == ""
    assert o.post("/war/box/unlist", data={"csrf_token": token, "national_id": "A200003", "box": "Box B",
                                           "to": "Made Up Box"}).status_code == 400
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    assert "Not at this box" not in t1.get("/war/box").get_data(as_text=True)           # not for box agents
    assert t1.post("/war/box/unlist", data={"csrf_token": csrf(t1, "/war/box"), "national_id": "A200001",
                                            "box": "Box A"}).status_code in (302, 403)
    assert conn.execute("SELECT COUNT(*) FROM war_list").fetchone()[0] == 1
