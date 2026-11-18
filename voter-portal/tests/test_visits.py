"""House visits and where voters live."""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import hash_password  # noqa: E402
from app.db import connect, now_iso  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402

ROSE = "Rose Villa"   # A200001 Aminath Alpha, A200003
BLUE = "Blue Ge"      # A200002 Moosa Beta


def own_status(app, nid, username):
    row = connect(app.config["DATABASE"]).execute(
        "SELECT c.status FROM canvass c JOIN users u ON u.id = c.user_id WHERE c.national_id = ? AND u.username = ?",
        (nid, username)).fetchone()
    return row[0] if row else "new"


def visit(client, house, outcome, met=(), follow_redirects=False, **extra):
    data = {"csrf_token": csrf(client, "/visits"), "h": house, "outcome": outcome, "met": list(met), **extra}
    return client.post("/visits/house", data=data, follow_redirects=follow_redirects)


def test_record_visits_update_own_status(app):
    u = app.test_client()
    login(u, "viewer", USER_PW)
    page = u.get("/visits").get_data(as_text=True)
    assert ROSE in page and BLUE in page and "Never visited" in page
    house = u.get("/visits/house?h=Rose+Villa").get_data(as_text=True)
    assert "Aminath Alpha" in house and "Record a visit" in house and "Moosa" not in house

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = visit(u, ROSE, "met", ["A200001", "A200002"], notes="Come after 8pm", next_visit_on=tomorrow, transport="1")
    assert r.status_code == 302
    assert own_status(app, "A200001", "viewer") == "contacted"
    assert own_status(app, "A200003", "viewer") == "new"          # not ticked: unchanged
    assert own_status(app, "A200002", "viewer") == "new"          # not living there: ignored
    house = u.get("/visits/house?h=Rose+Villa").get_data(as_text=True)
    assert "Met family" in house and "Come after 8pm" in house and "Needs transport" in house
    assert "Met: Aminath Alpha" in house

    # nobody home: only voters not reached yet become No answer
    visit(u, ROSE, "nobody")
    assert own_status(app, "A200001", "viewer") == "contacted" and own_status(app, "A200003", "viewer") == "noanswer"
    # come back later with nobody ticked: those not reached become Follow up
    visit(u, ROSE, "later")
    assert own_status(app, "A200001", "viewer") == "contacted" and own_status(app, "A200003", "viewer") == "followup"

    # the list shows the latest visit and the flags
    page = u.get("/visits").get_data(as_text=True)
    assert "Come back later" in page and "3 visits" in page
    assert u.get("/visits?f=never").get_data(as_text=True).count('class="house-row') == 1   # Blue Ge
    # still "to visit": Blue Ge (never) and Rose Villa (come back later, no date set)
    assert u.get("/visits?f=todo").get_data(as_text=True).count('class="house-row') == 2
    assert u.get("/visits?f=transport").get_data(as_text=True).count('class="house-row') == 0  # latest visit has none

    # refused input
    assert visit(u, ROSE, "bogus").status_code == 400
    assert visit(u, "No Such House", "met").status_code == 404
    r = visit(u, ROSE, "met", follow_redirects=True)
    assert "Tick who you met" in r.get_data(as_text=True)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    r = visit(u, ROSE, "nobody", next_visit_on=yesterday, follow_redirects=True)
    assert "can&#39;t be in the past" in r.get_data(as_text=True) or "can't be in the past" in r.get_data(as_text=True)
    n = connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM house_visits").fetchone()[0]
    assert n == 3


def test_visits_are_personal_admins_see_all(app):
    conn = connect(app.config["DATABASE"])
    stamp = now_iso(precise=True)
    conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                 " created_at) VALUES ('other', ?, 'user', 0, ?, ?)", (hash_password(USER_PW), stamp, stamp))
    conn.commit()
    u = app.test_client()
    login(u, "viewer", USER_PW)
    visit(u, BLUE, "met", ["A200002"], notes="secret note")
    o = app.test_client()
    login(o, "other", USER_PW)
    assert "secret note" not in o.get("/visits/house?h=Blue+Ge").get_data(as_text=True)
    assert "Never visited" in o.get("/visits").get_data(as_text=True)
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    house = a.get("/visits/house?h=Blue+Ge").get_data(as_text=True)
    assert "secret note" in house and "viewer" in house
    visit_id = connect(app.config["DATABASE"]).execute("SELECT id FROM house_visits").fetchone()[0]
    assert u.post(f"/visits/{visit_id}/delete", data={"csrf_token": csrf(u, "/")}).status_code == 403
    a.post(f"/visits/{visit_id}/delete", data={"csrf_token": csrf(a, "/")})
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM house_visits").fetchone()[0] == 0
    csv_text = a.get("/visits/export.csv").get_data(as_text=True)
    assert csv_text.lstrip("﻿").startswith("Visited,House,By,Outcome")
    assert u.get("/visits/export.csv").status_code == 403


def test_lives_at_moves_voters_out_of_house_visits(app):
    u = app.test_client()
    login(u, "viewer", USER_PW)
    token = csrf(u, "/")
    u.post("/voters/A200003/lives", data={"csrf_token": token, "lives_at": "male", "lives_detail": "Henveiru",
                                          "back": "/visits/house?h=Rose+Villa"})
    house = u.get("/visits/house?h=Rose+Villa").get_data(as_text=True)
    assert "Registered here, living elsewhere" in house and "Malé · Henveiru" in house
    assert len(re.findall(r'name="met" value="(A\d+)"', house)) == 1                 # only Aminath lives there
    visit(u, ROSE, "nobody")
    assert own_status(app, "A200003", "viewer") == "new"                             # away: not touched

    # Aminath now stays at Blue Ge: she shows up there, and Rose Villa has nobody living in it
    u.post("/voters/A200001/lives", data={"csrf_token": token, "lives_at": "house", "lives_detail": BLUE})
    blue = u.get("/visits/house?h=Blue+Ge").get_data(as_text=True)
    assert set(re.findall(r'name="met" value="(A\d+)"', blue)) == {"A200001", "A200002"}
    assert "registered at Rose Villa" in blue
    assert u.post("/voters/A200001/lives", data={"csrf_token": token, "lives_at": "house"}).status_code == 302
    assert u.post("/voters/A200001/lives", data={"csrf_token": token, "lives_at": "moon"}).status_code == 400

    # the Away list and the voter card
    page = u.get("/?lv=away").get_data(as_text=True)
    assert page.count('id="v-A2000') == 1 and "Lives away" in page
    assert u.get("/?lv=house").get_data(as_text=True).count('id="v-A2000') == 1
    assert "Lives at" in u.get("/voters/A200003").get_data(as_text=True)

    # admin dashboard: house progress counts only people living in the house; away count shown
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    dash = a.get("/dashboard").get_data(as_text=True)
    # Rose Villa: nobody lives there now; Blue Ge: Moosa (Aminath stays there but is registered elsewhere)
    assert "Live away" in dash and "0 of 1" in dash and ROSE not in dash.split("Most active")[0].split("house-list")[-1]
    header, *lines = a.get("/export.csv").get_data(as_text=True).lstrip("﻿").strip().splitlines()
    assert header.endswith("Lives at") or ",Lives at" in header
    assert any("Malé · Henveiru" in line for line in lines)
    assert "voter_lives" in a.get("/admin/audit").get_data(as_text=True)


def test_agents_only_get_their_houses(app):
    ali = add_agent(app, "ali")
    conn = connect(app.config["DATABASE"])
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, 't')", (BLUE, ali))
    conn.commit()
    g = app.test_client()
    login(g, "ali", AGENT_PW)
    page = g.get("/visits").get_data(as_text=True)
    assert BLUE in page and ROSE not in page
    assert g.get("/visits/house?h=Rose+Villa").status_code == 404
    assert visit(g, ROSE, "nobody").status_code == 404
    assert visit(g, BLUE, "met", ["A200002"]).status_code == 302
    assert own_status(app, "A200002", "ali") == "contacted"
    my = g.get("/my").get_data(as_text=True)
    assert "Next houses to visit" not in my or BLUE in my
