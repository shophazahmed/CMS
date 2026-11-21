"""Candidate pledge sheet and how the vote splits."""
import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402
from tests.test_confirmed import OWNER_PW, add_owner  # noqa: E402
# fixture voters: A200001 Aminath Alpha (F 30, Rose Villa), A200002 Moosa Beta (M 55, Blue Ge), A200003 (M 70, Rose Villa)


def setup(client, names=("Shoppe", "Rayya", "Jade")):
    token = csrf(client, "/pledges/candidates")
    for n in names:
        client.post("/pledges/candidates", data={"csrf_token": token, "action": "add", "name": n})
    ids = {r["name"]: r["id"] for r in connect(client.application.config["DATABASE"]).execute("SELECT id, name FROM candidates")}
    return token, ids


def mark(client, token, nid, cid, value):
    return client.post("/pledges/set", data={"csrf_token": token, "national_id": nid, "candidate_id": cid, "value": value},
                       headers={"X-Requested-With": "fetch"})


def test_sheet_marks_totals_and_filters(app):
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    assert "No candidates yet" in a.get("/pledges").get_data(as_text=True)
    token, ids = setup(a)
    assert "already on the list" in a.post("/pledges/candidates", data={"csrf_token": token, "action": "add", "name": "jade"},
                                          follow_redirects=True).get_data(as_text=True)
    r = mark(a, token, "A200001", ids["Shoppe"], "yes")
    assert r.json == {"ok": True, "value": "yes", "label": "Yes"}
    mark(a, token, "A200001", ids["Rayya"], "yes")
    mark(a, token, "A200002", ids["Shoppe"], "maybe")
    mark(a, token, "A200003", ids["Jade"], "no")
    assert mark(a, token, "A200003", ids["Jade"], "bogus").status_code == 400
    assert mark(a, token, "A999999", ids["Jade"], "yes").status_code == 404

    page = a.get("/pledges").get_data(as_text=True)
    assert "Shoppe" in page and "Rayya" in page and page.count('class="pl pl-yes"') == 2
    assert page.count('class="pl pl-maybe"') == 1 and page.count('class="pl pl-no"') == 1
    assert "1 maybe" in page                                                   # Shoppe: 1 yes, 1 maybe
    only = a.get(f"/pledges?pc={ids['Shoppe']}&pv=yes").get_data(as_text=True)
    assert "Aminath Alpha" in only and "Moosa Beta" not in only
    none = a.get("/pledges?pc=any&pv=none").get_data(as_text=True)              # no Yes for anyone
    assert "Moosa Beta" in none and "Aminath Alpha</a>" not in none and "<strong>2</strong> voters" in none
    assert ".cand-" in a.get("/pledges/colors.css").get_data(as_text=True)
    # clearing a mark
    mark(a, token, "A200003", ids["Jade"], "")
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM pledges").fetchone()[0] == 3
    assert "pledge" in a.get("/admin/audit").get_data(as_text=True)


def test_distribution(app):
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Gdh. Gadhdhoo'")
    conn.commit()
    token, ids = setup(a)
    mark(a, token, "A200001", ids["Shoppe"], "yes")
    mark(a, token, "A200001", ids["Rayya"], "yes")
    mark(a, token, "A200002", ids["Shoppe"], "yes")
    mark(a, token, "A200003", ids["Jade"], "maybe")
    page = a.get("/pledges/distribution").get_data(as_text=True)
    assert "How the vote splits" in page and "Shared supporters" in page
    assert "66.7% of voters" in page                    # Shoppe: 2 of 3
    assert "1 voter pledged" not in page and "2 voters pledged Yes to at least one candidate" in page
    assert "1 to more than one" in page
    assert "33.3% of voters have no Yes for anyone" in page
    assert "Gdh. Gadhdhoo" in page and "Women" in page
    assert "A voter can vote for 2 candidates" in page                     # the default limit
    # only one allowed: the double pledge is flagged
    a.post("/pledges/candidates", data={"csrf_token": token, "action": "max_votes", "max_votes": "1"})
    flagged = " ".join(a.get("/pledges/distribution").get_data(as_text=True).split())
    assert "pledged Yes to more than 1 candidate, but a voter can vote for only 1" in flagged
    # filters narrow it down
    women = a.get("/pledges/distribution?sex=F").get_data(as_text=True)
    assert "Yes pledges among 1 voter" in women


def test_export_and_import(app):
    from openpyxl import Workbook, load_workbook

    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    token, ids = setup(a, ("Shoppe", "Rayya", "Jade", "Thalaal"))
    # an existing sheet like the campaign's: a title row, then the header
    wb = Workbook()
    ws = wb.active
    ws.append(["Gadhdhoo pledge sheet"])
    ws.append(["#", "ID", "Full name", "Shoppe", "Rayya", "Jade", "Thalaal", "Notes"])
    ws.append([1, "A200001", "Aminath Alpha", "yes", "", "Yes", None, "x"])
    ws.append([2, "a200002", "Moosa Beta", "", "maybe", "no", "?", ""])
    ws.append([3, "A999999", "Nobody", "yes", "", "", "", ""])
    ws.append([4, "A200003", "Third", "later", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    r = a.post("/pledges/import", data={"csrf_token": token, "sheet": (io.BytesIO(buf.getvalue()), "sheet.xlsx")},
               content_type="multipart/form-data", follow_redirects=True)
    text = r.get_data(as_text=True)
    assert "Imported 5 pledge marks" in text and "1 row had an ID" in text and "weren&#39;t yes / maybe / no" in text
    marks = {(n, c): v for n, c, v in connect(app.config["DATABASE"]).execute(
        "SELECT national_id, candidate_id, value FROM pledges")}
    assert marks[("A200001", ids["Shoppe"])] == "yes" and marks[("A200001", ids["Jade"])] == "yes"
    assert marks[("A200002", ids["Rayya"])] == "maybe" and marks[("A200002", ids["Jade"])] == "no"
    assert marks[("A200002", ids["Thalaal"])] == "maybe"

    rows = list(csv.reader(io.StringIO(a.get("/pledges/export.csv").get_data(as_text=True).lstrip("﻿"))))
    assert rows[0][-5:] == ["No-show", "Shoppe", "Rayya", "Jade", "Thalaal"] and "100% confirmed" not in rows[0]
    alpha = dict(zip(rows[0], next(r for r in rows[1:] if r[1] == "A200001")))
    assert (alpha["Shoppe"], alpha["Jade"], alpha["Rayya"]) == ("yes", "yes", "")
    ws = load_workbook(io.BytesIO(a.get("/pledges/export.xlsx").data)).active
    assert ws["B1"].value == "ID" and ws.max_row == 4
    # bad import files
    r = a.post("/pledges/import", data={"csrf_token": token, "sheet": (io.BytesIO(b"x,y\n1,2"), "s.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "No “ID” column" in r.get_data(as_text=True)


def test_access_is_given_by_the_owner(app):
    ali = add_agent(app, "ali")
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token, ids = setup(o)
    mark(o, token, "A200001", ids["Shoppe"], "yes")
    conn = connect(app.config["DATABASE"])
    boss, viewer = (conn.execute("SELECT id FROM users WHERE username = ?", (n,)).fetchone()[0] for n in ("boss", "viewer"))

    # nobody else has access yet, not even another admin
    for user, pw in (("boss", ADMIN_PW), ("viewer", USER_PW), ("ali", AGENT_PW)):
        c = app.test_client()
        login(c, user, pw)
        for path in ("/pledges", "/pledges/distribution", "/pledges/candidates", "/pledges/export.csv"):
            assert c.get(path).status_code == 403, (user, path)
        assert c.post("/pledges/set", data={"csrf_token": csrf(c, "/")}).status_code == 403
        assert "Pledges" not in c.get("/").get_data(as_text=True).split("</nav>")[0]

    # the owner gives "view" to the read-only user and "mark" to the other admin (Users page)
    users_page = o.get("/admin/users").get_data(as_text=True)
    assert "Pledge sheet" in users_page and "set_pledge_access" in users_page
    o.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": "set_pledge_access", "level": "view"})
    o.post(f"/admin/users/{boss}", data={"csrf_token": token, "action": "set_pledge_access", "level": "mark"})
    v = app.test_client()
    login(v, "viewer", USER_PW)
    page = v.get("/pledges").get_data(as_text=True)
    assert "ask the owner" in page and "data-pledge-form" not in page and "pl pl-yes" in page
    assert v.get("/pledges/distribution").status_code == 200
    assert mark(v, csrf(v, "/"), "A200002", ids["Shoppe"], "yes").status_code == 403
    for path in ("/pledges/candidates", "/pledges/export.csv"):
        assert v.get(path).status_code == 403
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert "data-pledge-form" in b.get("/pledges").get_data(as_text=True)
    assert mark(b, csrf(b, "/"), "A200002", ids["Shoppe"], "yes").status_code == 200
    assert b.get("/pledges/export.csv").status_code == 403                       # downloads stay with the owner
    assert "Candidates &amp; import" not in b.get("/pledges").get_data(as_text=True)
    # only the owner hands out access
    assert b.post(f"/admin/users/{viewer}", data={"csrf_token": csrf(b, "/"), "action": "set_pledge_access",
                                                  "level": "mark"}).status_code == 403
    assert "pledge_access" in o.get("/admin/audit").get_data(as_text=True)


def test_dashboard_shows_candidates(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Gdh. Gadhdhoo' WHERE house_name = 'Rose Villa'")
    conn.execute("UPDATE voters SET location = 'ViliMale, Ehenihen' WHERE house_name = 'Blue Ge'")
    conn.commit()
    token, ids = setup(o, ("Shoppe", "Rayya"))
    mark(o, token, "A200001", ids["Shoppe"], "yes")
    mark(o, token, "A200002", ids["Rayya"], "yes")
    dash = o.get("/dashboard").get_data(as_text=True)
    assert "How the vote splits" in dash and "Shoppe" in dash and "33.3% of 3 voters" in dash
    assert "Not pledged yet" in dash
    boxed = o.get("/dashboard", query_string={"box": "ViliMale, Ehenihen"}).get_data(as_text=True)
    assert "100.0% of 1 voters" in boxed                                        # Rayya at that box
    # an admin without pledge access sees no pledge tiles
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert "How the vote splits" not in b.get("/dashboard").get_data(as_text=True)


def test_two_candidates_per_voter_and_no_shows(app):
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    token, ids = setup(a)
    assert ">100% confirmed</th>" not in a.get("/pledges").get_data(as_text=True)    # column removed
    assert mark(a, token, "A200001", ids["Shoppe"], "yes").status_code == 200
    assert mark(a, token, "A200001", ids["Rayya"], "yes").status_code == 200
    r = mark(a, token, "A200001", ids["Jade"], "yes")                                # a third Yes is refused
    assert r.status_code == 409 and "can vote for 2 candidates" in r.json["error"]
    assert mark(a, token, "A200001", ids["Jade"], "maybe").status_code == 200        # a Maybe is fine
    assert mark(a, token, "A200001", ids["Shoppe"], "yes").status_code == 200        # re-saving an existing Yes too
    a.post("/pledges/candidates", data={"csrf_token": token, "action": "max_votes", "max_votes": "0"})
    assert mark(a, token, "A200001", ids["Jade"], "yes").status_code == 200          # no limit

    # no-show: mark, filter, totals, distribution, export
    r = a.post("/pledges/noshow", data={"csrf_token": token, "national_id": "A200001", "value": "1"},
               headers={"X-Requested-With": "fetch"})
    assert r.json == {"ok": True, "noshow": True}
    page = a.get("/pledges").get_data(as_text=True)
    assert "No-show" in page and 'class="pl pl-ns"' in page
    assert "Aminath Alpha" in a.get("/pledges?ns=1").get_data(as_text=True)
    assert "Aminath Alpha</a>" not in a.get("/pledges?ns=0").get_data(as_text=True)
    dist = a.get("/pledges/distribution").get_data(as_text=True)
    assert "1 no-show (can" in dist and "<strong>0</strong> expected to vote" in dist   # her pledges aren't expected
    rows = list(csv.reader(io.StringIO(a.get("/pledges/export.csv").get_data(as_text=True).lstrip("\ufeff"))))
    assert dict(zip(rows[0], next(r for r in rows[1:] if r[1] == "A200001")))["No-show"] == "yes"
    assert "No-show" in a.get("/dashboard").get_data(as_text=True)
    a.post("/pledges/noshow", data={"csrf_token": token, "national_id": "A200001", "value": "0"})
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM pledge_noshow").fetchone()[0] == 0
    # import a No-show column
    sheet = "ID,Shoppe,No-show\nA200002,yes,yes\n"
    a.post("/pledges/import", data={"csrf_token": token, "sheet": (io.BytesIO(sheet.encode()), "s.csv")},
           content_type="multipart/form-data")
    assert [r[0] for r in connect(app.config["DATABASE"]).execute("SELECT national_id FROM pledge_noshow")] == ["A200002"]
    # viewers can't mark no-shows
    boss = connect(app.config["DATABASE"]).execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    a.post(f"/admin/users/{boss}", data={"csrf_token": token, "action": "set_pledge_access", "level": "view"})
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.post("/pledges/noshow", data={"csrf_token": csrf(b, "/"), "national_id": "A200001", "value": "1"}).status_code == 403
    assert "data-noshow-form" not in b.get("/pledges").get_data(as_text=True)
