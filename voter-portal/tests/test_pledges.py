"""Candidate pledge sheet and how the vote splits."""
import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402
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
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
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
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
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
    # only one allowed: the double pledge is flagged
    a.post("/pledges/candidates", data={"csrf_token": token, "action": "mode", "mode": "one"})
    assert "pledged Yes to more than one candidate, but voters can vote for only one" in \
        a.get("/pledges/distribution").get_data(as_text=True)
    # filters narrow it down
    women = a.get("/pledges/distribution?sex=F").get_data(as_text=True)
    assert "Yes pledges among 1 voter" in women


def test_export_and_import(app):
    from openpyxl import Workbook, load_workbook

    a = app.test_client()
    login(a, "boss", ADMIN_PW)
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
    assert rows[0][-4:] == ["Shoppe", "Rayya", "Jade", "Thalaal"] and "100% confirmed" not in rows[0]   # not the owner
    alpha = dict(zip(rows[0], next(r for r in rows[1:] if r[1] == "A200001")))
    assert (alpha["Shoppe"], alpha["Jade"], alpha["Rayya"]) == ("yes", "yes", "")
    ws = load_workbook(io.BytesIO(a.get("/pledges/export.xlsx").data)).active
    assert ws["B1"].value == "ID" and ws.max_row == 4
    # bad import files
    r = a.post("/pledges/import", data={"csrf_token": token, "sheet": (io.BytesIO(b"x,y\n1,2"), "s.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert "No “ID” column" in r.get_data(as_text=True)


def test_admins_only(app):
    add_agent(app, "ali")
    for user, pw in (("viewer", USER_PW), ("ali", AGENT_PW)):
        c = app.test_client()
        login(c, user, pw)
        for path in ("/pledges", "/pledges/distribution", "/pledges/candidates", "/pledges/export.csv"):
            assert c.get(path).status_code == 403, path
        assert c.post("/pledges/set", data={"csrf_token": csrf(c, "/")}).status_code == 403
