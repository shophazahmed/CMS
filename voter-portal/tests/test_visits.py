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


def test_house_page_box_filter(app):
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Gdh. Gadhdhoo', phone = '7773680' WHERE national_id = 'A200001'")
    conn.execute("UPDATE voters SET location = ? WHERE national_id = 'A200003'", ("GDH. Atoll, Male (Male' hub)",))
    conn.commit()
    u = app.test_client()
    login(u, "viewer", USER_PW)
    page = u.get("/visits/house?h=Rose+Villa").get_data(as_text=True)
    assert "Box (where they live and vote)" in page and "Living here" not in page
    assert "7773680</span> · Gdh. Gadhdhoo" in page                                  # phone · box on each row
    assert len(re.findall(r'name="met" value="(A\d+)"', page)) == 2
    male = u.get("/visits/house", query_string={"h": ROSE, "box": "GDH. Atoll, Male (Male' hub)"}).get_data(as_text=True)
    assert re.findall(r'name="met" value="(A\d+)"', male) == ["A200003"]
    # with a box chosen, "nobody home" only touches the voters shown
    visit(u, ROSE, "nobody", box="GDH. Atoll, Male (Male' hub)")
    assert own_status(app, "A200003", "viewer") == "noanswer" and own_status(app, "A200001", "viewer") == "new"
    assert u.post("/voters/A200001/lives", data={"csrf_token": csrf(u, "/"), "lives_at": "male"}).status_code == 404


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


def test_box_filter_and_voter_count_sorts(app):
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Gadhdhoo' WHERE house_name = ?", (ROSE,))
    conn.execute("UPDATE voters SET location = 'Male 1' WHERE house_name = ?", (BLUE,))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    houses = lambda path: re.findall(r'class="house-name">([^<]+)<', a.get(path).get_data(as_text=True))  # noqa: E731
    page = a.get("/visits").get_data(as_text=True)
    assert "Gadhdhoo (1)" in page and "Male 1 (1)" in page                       # box dropdown
    assert houses("/visits?box=Male+1") == [BLUE]
    assert houses("/visits?box=Gadhdhoo&f=never") == [ROSE]
    assert "Never visited <b>1</b>" in a.get("/visits?box=Gadhdhoo").get_data(as_text=True)  # chips follow the box
    assert houses("/visits?box=(none)") == []
    assert houses("/visits?sort=box") == [ROSE, BLUE]                             # Gadhdhoo before Male 1
    assert houses("/visits?sort=voters") == [ROSE, BLUE]                          # 2 voters, then 1
    assert houses("/visits?sort=fewest") == [BLUE, ROSE]


def test_box_names_with_commas_and_dashboard_box_filter(app):
    box = "GDH. Atoll, Male (Male' hub)"
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = ? WHERE house_name = ?", (box, BLUE))
    conn.execute("UPDATE voters SET location = 'Gdh. Gadhdhoo' WHERE house_name = ?", (ROSE,))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    page = a.get("/visits").get_data(as_text=True)
    assert "GDH. Atoll, Male (Male&#39; hub) (1)" in page and ">GDH. Atoll (" not in page   # one box, not two
    assert re.findall(r'class="house-name">([^<]+)<', a.get("/visits", query_string={"box": box}).get_data(as_text=True)) == [BLUE]
    dash = a.get("/dashboard", query_string={"box": box}).get_data(as_text=True)
    assert "Box (where they vote)" in dash and "0 of 1" in dash and "box GDH. Atoll, Male" in dash
    houses = dash.split("Most active")[0]
    assert BLUE in houses and ROSE not in houses.split("house-list")[-1]
    assert "0 of 2" in a.get("/dashboard?box=Gdh.+Gadhdhoo").get_data(as_text=True)


def test_my_dashboard_box_filter(app):
    ali = add_agent(app, "ali")
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Gdh. Gadhdhoo' WHERE house_name = ?", (ROSE,))
    conn.execute("UPDATE voters SET location = 'ViliMale, Ehenihen' WHERE house_name = ?", (BLUE,))
    for h in (ROSE, BLUE):
        conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, 't')", (h, ali))
    conn.commit()
    g = app.test_client()
    login(g, "ali", AGENT_PW)
    token = csrf(g, "/")
    g.post("/voters/A200001/canvass", data={"csrf_token": token, "status": "contacted"})
    g.post("/voters/A200002/canvass", data={"csrf_token": token, "status": "followup"})
    page = g.get("/my").get_data(as_text=True)
    assert "Box (where they vote)" in page and "ViliMale, Ehenihen (1)" in page
    assert "Aminath Alpha" in page and "Moosa Beta" in page
    page = g.get("/my", query_string={"box": "ViliMale, Ehenihen"}).get_data(as_text=True)
    recent = page.split("Latest")[1].split("</ul>")[0] if "Latest" in page else page
    assert "Moosa Beta" in recent and "Aminath Alpha" not in recent
    assert "· ViliMale, Ehenihen</h2>" in page
    houses = re.findall(r'class="house-name">(?:✓ )?([^<]+)<', page)
    assert BLUE in houses and ROSE not in houses
    assert "loc=ViliMale" in page                                             # links keep the box


def test_export_house_list(app):
    import csv as _csv
    import io as _io

    from openpyxl import load_workbook

    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Gdh. Gadhdhoo' WHERE house_name = ?", (ROSE,))
    conn.execute("UPDATE voters SET location = ? WHERE house_name = ?", ("GDH. Atoll, Male (Male' hub)", BLUE))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    visit(a, ROSE, "met", ["A200001"], next_visit_on=tomorrow, transport="1")
    page = a.get("/visits").get_data(as_text=True)
    assert "/visits/houses.csv" in page and "/visits/houses.xlsx" in page

    rows = list(_csv.reader(_io.StringIO(a.get("/visits/houses.csv?sort=name").get_data(as_text=True).lstrip("﻿"))))
    assert rows[0][:6] == ["#", "House", "Box", "Voters at box", "Other boxes (voters)", "Voters"]
    by_house = {r[1]: r for r in rows[1:]}
    assert set(by_house) == {ROSE, BLUE}
    rose = dict(zip(rows[0], by_house[ROSE]))
    assert (rose["Box"], rose["Voters"], rose["Reached"], rose["Visits"], rose["Last outcome"]) == \
        ("Gdh. Gadhdhoo", "2", "1", "1", "Met family")
    assert rose["Next visit"] == tomorrow and rose["Needs transport"] == "yes" and rose["Last visit by"] == "boss"
    assert dict(zip(rows[0], by_house[BLUE]))["Due"] == "Never visited"
    assert dict(zip(rows[0], by_house[BLUE]))["Box"] == "GDH. Atoll, Male (Male' hub)"
    assert rose["Voters at box"] == "2" and rose["Other boxes (voters)"] == ""
    # a house split over two boxes: main box with its count, the other box with its count
    conn.execute("UPDATE voters SET location = ? WHERE national_id = 'A200003'", ("GDH. Atoll, Male (Male' hub)",))
    conn.execute("UPDATE voters SET location = 'Gdh. Gadhdhoo' WHERE national_id = 'A200001'")
    conn.execute("INSERT INTO voters (national_id, full_name, house_name, location, created_at, updated_at)"
                 " VALUES ('A200009', 'Extra', 'Rose Villa', 'Gdh. Gadhdhoo', 't', 't')")
    conn.commit()
    rows = list(_csv.reader(_io.StringIO(a.get("/visits/houses.csv").get_data(as_text=True).lstrip("\ufeff"))))
    rose = dict(zip(rows[0], next(r for r in rows[1:] if r[1] == ROSE)))
    assert (rose["Box"], rose["Voters at box"], rose["Other boxes (voters)"], rose["Voters"]) == \
        ("Gdh. Gadhdhoo", "2", "GDH. Atoll, Male (Male' hub) (1)", "3")
    # with a box chosen, that box's count comes first
    rows = list(_csv.reader(_io.StringIO(a.get("/visits/houses.csv", query_string={
        "box": "GDH. Atoll, Male (Male' hub)"}).get_data(as_text=True).lstrip("\ufeff"))))
    rose = dict(zip(rows[0], next(r for r in rows[1:] if r[1] == ROSE)))
    assert (rose["Box"], rose["Voters at box"], rose["Other boxes (voters)"]) == \
        ("GDH. Atoll, Male (Male' hub)", "1", "Gdh. Gadhdhoo (2)")

    # follows the filters on the page
    only = list(_csv.reader(_io.StringIO(a.get("/visits/houses.csv?f=never").get_data(as_text=True).lstrip("﻿"))))
    assert [r[1] for r in only[1:]] == [BLUE]
    boxed = a.get("/visits/houses.csv", query_string={"box": "Gdh. Gadhdhoo"}).get_data(as_text=True)
    assert ROSE in boxed and BLUE not in boxed
    ws = load_workbook(_io.BytesIO(a.get("/visits/houses.xlsx").data)).active
    assert ws["B1"].value == "House" and ws.max_row == 3
    assert a.get("/visits/houses.pdf").status_code == 404
    assert "visits_houses_export" in a.get("/admin/audit").get_data(as_text=True)

    # admins only
    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert u.get("/visits/houses.csv").status_code == 403
    assert "/visits/houses.csv" not in u.get("/visits").get_data(as_text=True)


def test_visits_order_and_house_page_sort(app):
    conn = connect(app.config["DATABASE"])
    conn.execute("INSERT INTO voters (national_id, full_name, age, house_name, created_at, updated_at)"
                 " VALUES ('A200009', 'Zahir Young', 19, 'Rose Villa', 't', 't')")
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    houses = lambda q: re.findall(r'class="house-name">([^<]+)<', a.get("/visits" + q).get_data(as_text=True))  # noqa: E731
    assert houses("?sort=name") == [BLUE, ROSE]
    assert houses("?sort=name&dir=desc") == [ROSE, BLUE]                      # Z to A
    assert houses("?sort=voters") == [ROSE, BLUE] and houses("?sort=voters&dir=desc") == [BLUE, ROSE]
    page = a.get("/visits?sort=name&dir=desc").get_data(as_text=True)
    assert "House Z to A" in page and 'name="dir" value="desc"' in page        # kept by the search box
    import csv as _csv
    import io as _io
    rows = list(_csv.reader(_io.StringIO(a.get("/visits/houses.csv?sort=name&dir=desc").get_data(as_text=True).lstrip("﻿"))))
    assert [r[1] for r in rows[1:]] == [ROSE, BLUE]                            # export follows

    met = lambda q: re.findall(r'name="met" value="(A\d+)"', a.get("/visits/house?h=Rose+Villa" + q).get_data(as_text=True))  # noqa: E731
    assert met("") == ["A200003", "A200001", "A200009"]                       # oldest first: 70, 30, 19
    assert met("&vs=young") == ["A200009", "A200001", "A200003"]
    assert met("&vs=name") == ["A200003", "A200001", "A200009"]               # '=HYPERLINK(1)', Aminath, Zahir
    assert met("&vs=name_desc") == ["A200009", "A200001", "A200003"]
    assert "Youngest first" in a.get("/visits/house?h=Rose+Villa").get_data(as_text=True)
