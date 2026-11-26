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
    assert rows[0][-6:] == ["No-show", "Transport", "Shoppe", "Rayya", "Jade", "Thalaal"] and "100% confirmed" not in rows[0]
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
    assert "Leaning" in dash and "Undecided" in dash
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


def test_sort_by_name_and_house(app):
    import re as _re

    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    setup(a, ("Shoppe",))
    names = lambda q: _re.findall(r'<td class="sticky"><a [^>]+>.*?<span>([^<]+)</span></a>',  # noqa: E731
                                  a.get("/pledges" + q).get_data(as_text=True), _re.S)
    assert names("") == ["=HYPERLINK(1)", "Aminath Alpha", "Moosa Beta"]          # name, A to Z
    assert names("?sort=name&dir=desc") == ["Moosa Beta", "Aminath Alpha", "=HYPERLINK(1)"]
    assert names("?sort=house")[0] == "Moosa Beta"                                 # Blue Ge before Rose Villa
    assert names("?sort=house&dir=desc")[-1] == "Moosa Beta"
    assert names("?sort=age&dir=desc")[0] == "=HYPERLINK(1)"                       # 70 years old
    page = a.get("/pledges?sort=house").get_data(as_text=True)
    assert 'aria-sort="ascending"' in page and "sort=house&amp;dir=desc" in page    # tap again for Z to A
    assert 'name="sort" value="house"' in page                                     # filters keep the sort
    assert names("?sort=bogus") == names("")
    rows = list(csv.reader(io.StringIO(a.get("/pledges/export.csv?sort=name&dir=desc").get_data(as_text=True).lstrip("\ufeff"))))
    assert [r[2] for r in rows[1:]] == ["Moosa Beta", "Aminath Alpha", "'=HYPERLINK(1)"]   # export follows the sort


def test_sheet_shows_photos(app):
    add_owner(app)
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET photo_path = 'A200001.jpg' WHERE national_id = 'A200001'")
    conn.commit()
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    setup(a, ("Shoppe",))
    page = a.get("/pledges").get_data(as_text=True)
    assert 'class="avatar xs" src="/photos/A200001"' in page                       # photo
    assert page.count('class="avatar xs initials"') == 2                          # initials for the others


def test_pledge_sheet_only_account(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token, ids = setup(o, ("Shoppe", "Rayya"))
    # the owner creates a "pledge sheet only" account
    assert "Pledge sheet only" in o.get("/admin/users").get_data(as_text=True)
    o.post("/admin/users", data={"csrf_token": token, "username": "pledger", "role": "pledge",
                                 "password": "pledger-pass-123"})
    row = connect(app.config["DATABASE"]).execute(
        "SELECT role, pledge_only, pledge_access, id FROM users WHERE username = 'pledger'").fetchone()
    assert tuple(row)[:3] == ("user", 1, "mark")
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE users SET must_change_password = 0 WHERE username = 'pledger'")
    conn.commit()

    p = app.test_client()
    r = login(p, "pledger", "pledger-pass-123")
    assert p.get(r.headers["Location"], follow_redirects=True).request.path == "/pledges"   # lands on the sheet
    page = p.get("/pledges").get_data(as_text=True)
    nav = page.split("</header>")[0]
    assert "Pledge sheet" in nav and "Distribution" in nav
    for hidden in ("Search", "Visits", "Dashboard", "Announcements", "My dashboard"):
        assert f">{hidden}<" not in nav, hidden
    assert "/voters/A200001" not in page                                            # names aren't links
    assert p.get("/pledges/distribution").status_code == 200
    assert mark(p, csrf(p, "/pledges"), "A200001", ids["Shoppe"], "yes").status_code == 200
    # everything else is out of reach
    for path in ("/", "/visits", "/voters/A200001", "/my", "/announcements", "/visits/house?h=Rose+Villa"):
        r = p.get(path)
        assert r.status_code == 302 and r.headers["Location"].endswith("/pledges"), path
    assert p.post("/voters/A200001/canvass", data={"csrf_token": csrf(p, "/pledges"), "status": "contacted"}).status_code == 403
    for path in ("/pledges/export.csv", "/pledges/candidates"):                    # owner-only pages: back to the sheet
        assert p.get(path).headers["Location"].endswith("/pledges"), path
    assert p.get("/account/password").status_code == 200

    # the owner can switch an existing user to "pledge sheet only" and back
    viewer = connect(app.config["DATABASE"]).execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    o.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": "set_pledge_only", "on": "1"})
    v = app.test_client()
    login(v, "viewer", USER_PW)
    assert v.get("/").status_code == 302 and v.get("/pledges").status_code == 200   # given View automatically
    assert "data-pledge-form" not in v.get("/pledges").get_data(as_text=True)       # view only
    o.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": "set_pledge_only", "on": "0"})
    assert v.get("/").status_code == 200
    # nobody but the owner can do it
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.post(f"/admin/users/{viewer}", data={"csrf_token": csrf(b, "/"), "action": "set_pledge_only",
                                                  "on": "1"}).status_code == 403
    assert b.post("/admin/users", data={"csrf_token": csrf(b, "/"), "username": "sneaky", "role": "pledge",
                                        "password": "sneaky-pass-1234"}, follow_redirects=True).status_code == 200
    assert connect(app.config["DATABASE"]).execute("SELECT 1 FROM users WHERE username = 'sneaky'").fetchone() is None


def test_distribution_sort_by_box(app):
    import re
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Box B'")
    conn.execute("UPDATE voters SET location = 'Box A' WHERE national_id = 'A200001'")
    conn.commit()
    token, ids = setup(a)
    mark(a, token, "A200001", ids["Shoppe"], "yes")        # Box A: 1 of 1 voter = 100%
    mark(a, token, "A200002", ids["Shoppe"], "yes")        # Box B: 1 of 2 = 50%, Rayya leads nowhere
    mark(a, token, "A200003", ids["Rayya"], "yes")
    mark(a, token, "A200002", ids["Rayya"], "yes")         # Box B: Rayya 2 pledges

    def order(qs=""):
        page = a.get("/pledges/distribution" + qs).get_data(as_text=True)
        body = page.split('id="by-box"')[1].split("</table>")[0]
        return [b for b in re.findall(r">(Box [AB])</a>", body)]

    assert order() == ["Box B", "Box A"]                           # most voters first by default
    assert order("?bs=box") == ["Box A", "Box B"]
    assert order("?bs=box&bd=desc") == ["Box B", "Box A"]
    assert order(f"?bs=p{ids['Shoppe']}") == ["Box A", "Box B"]    # highest share first
    assert order(f"?bs=c{ids['Rayya']}") == ["Box B", "Box A"]     # most pledges first
    assert order(f"?bs=c{ids['Rayya']}&bd=asc") == ["Box A", "Box B"]
    assert order("?bs=bogus&bd=sideways") == ["Box B", "Box A"]    # bad values fall back
    page = a.get("/pledges/distribution?bs=box&sex=F").get_data(as_text=True)
    assert 'name="bs" value="box"' in page                         # the filters keep the sort


def test_pledge_users_on_the_dashboard(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token, ids = setup(o, ("Shoppe", "Rayya"))
    o.post("/admin/users", data={"csrf_token": token, "username": "pledger", "role": "pledge",
                                 "password": "pledger-pass-123"})
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE users SET must_change_password = 0 WHERE username = 'pledger'")
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE username = 'pledger'").fetchone()[0]
    dash = o.get("/dashboard").get_data(as_text=True)
    assert "Pledge sheet users" in dash and "Never" in dash                          # not signed in yet
    p = app.test_client()
    login(p, "pledger", "pledger-pass-123")
    pt = csrf(p, "/pledges")
    mark(p, pt, "A200001", ids["Shoppe"], "yes")
    mark(p, pt, "A200002", ids["Rayya"], "yes")
    mark(p, pt, "A200002", ids["Rayya"], "maybe")
    p.post("/pledges/noshow", data={"csrf_token": pt, "national_id": "A200003", "value": "1"},
           headers={"X-Requested-With": "fetch"})
    mark(o, token, "A200001", ids["Shoppe"], "no")                                   # the owner changes one
    logged = conn.execute("SELECT COUNT(*) FROM pledge_log WHERE by_user = ?", (uid,)).fetchone()[0]
    assert logged == 4                                                               # kept apart from the pledges
    dash = " ".join(o.get("/dashboard").get_data(as_text=True).split())
    part = dash.split('aria-label="Pledge sheet users"')[1].split("</section>")[0]
    assert ">pledger</a>" in part
    cells = part.split(">pledger</a>")[1].split("</tr>")[0]
    # voters 3 · Shoppe 1 · Rayya 1 · maybe 1 · no 0 · no-show 1 · yes now 0 (Rayya became maybe, owner changed Shoppe)
    assert [c.split("<")[0] for c in cells.split("<td")[1:7]] == ['>3', '>1', '>1', '>1', '>0', '>1']
    assert "<strong>0</strong>" in cells and "Never" not in cells
    # tap the name: the voters they marked
    sheet = o.get(f"/pledges?pb={uid}").get_data(as_text=True)
    assert "Voters marked by <strong>pledger</strong>" in sheet and sheet.count("Aminath Alpha") >= 1
    # a date range in the future: nothing
    future = " ".join(o.get("/dashboard?from=2099-01-01&to=2099-01-02").get_data(as_text=True).split())
    cells = future.split(">pledger</a>")[1].split("</tr>")[0]
    assert cells.split("<td")[1].startswith(">0")
    # a plain admin who can't see pledges doesn't see the panel
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert "Pledge sheet users" not in b.get("/dashboard").get_data(as_text=True)


def test_solid_base_and_shared_supporters_lists(app):
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    token, ids = setup(a)
    mark(a, token, "A200001", ids["Shoppe"], "yes")
    mark(a, token, "A200001", ids["Rayya"], "yes")                 # shared Shoppe & Rayya
    mark(a, token, "A200002", ids["Shoppe"], "yes")                # only Shoppe
    mark(a, token, "A200003", ids["Jade"], "maybe")
    dist = a.get("/pledges/distribution").get_data(as_text=True)
    assert f"pp=only:{ids['Shoppe']}" in dist
    lo, hi = sorted((ids["Shoppe"], ids["Rayya"]))
    assert f"pp=both:{lo}-{hi}" in dist
    only = a.get(f"/pledges?pp=only:{ids['Shoppe']}").get_data(as_text=True)
    assert "Moosa Beta" in only and "Aminath Alpha</span>" not in only and "only Shoppe" in only
    both = a.get(f"/pledges?pp=both:{lo}-{hi}").get_data(as_text=True)
    assert "Aminath Alpha" in both and "Moosa Beta</span>" not in both and "Shared supporters" in both
    assert "Only Shoppe" in only and "Shoppe &amp; Rayya" in only or "Rayya &amp; Shoppe" in only   # the filter
    assert "Yes pledges among 1 voter" in a.get(f"/pledges/distribution?pp=only:{ids['Shoppe']}").get_data(as_text=True)
    assert "Aminath Alpha" in a.get("/pledges?pp=bogus").get_data(as_text=True)    # ignored


def test_agent_filter_on_the_pledge_sheet(app):
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    token, ids = setup(a)
    conn = connect(app.config["DATABASE"])
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    house = conn.execute("SELECT house_name FROM voters WHERE national_id = 'A200002'").fetchone()[0]
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, '2026-01-01')", (house, viewer))
    conn.commit()
    page = a.get("/pledges").get_data(as_text=True)
    assert 'name="ag"' in page and "/agents/sheet" in page              # the filter and the moved button
    only = a.get(f"/pledges?ag={viewer}").get_data(as_text=True)
    assert "Moosa Beta" in only and "Aminath Alpha</span>" not in only
    none = a.get("/pledges?ag=none").get_data(as_text=True)
    assert "Aminath Alpha" in none and "Moosa Beta</span>" not in none
    rows = a.get(f"/pledges/export.csv?ag={viewer}").get_data(as_text=True).strip().splitlines()
    assert len(rows) == 2                                                  # the export follows it
    dist = a.get(f"/pledges/distribution?ag={viewer}").get_data(as_text=True)
    assert "Yes pledges among 1 voter" in dist and "report-quick" in dist and f'value="{viewer}" selected' in dist



def test_connected_by_column(app):
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    token, ids = setup(a)
    mark(a, token, "A200001", ids["Shoppe"], "yes")
    a.post("/admin/users", data={"csrf_token": token, "username": "pledger", "role": "pledge", "password": "pledger-pass-123"})
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE users SET must_change_password = 0 WHERE username = 'pledger'")
    conn.commit()
    p = app.test_client()
    login(p, "pledger", "pledger-pass-123")
    mark(p, csrf(p, "/pledges"), "A200001", ids["Rayya"], "yes")
    page = " ".join(a.get("/pledges").get_data(as_text=True).split())
    assert "<th>Connector</th>" in page and 'placeholder="pledger, admin"' in page   # latest first, as a hint
    assert "<th>Connector</th>" not in p.get("/pledges").get_data(as_text=True)   # not for pledge-only accounts
    import csv as _csv
    rows = list(_csv.reader(io.StringIO(a.get("/pledges/export.csv").get_data(as_text=True).lstrip("\ufeff"))))
    i = rows[0].index("Connector")
    assert {r[1]: r[i] for r in rows[1:]}["A200001"] == "pledger, admin"
    # the agent who looks after the voter
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    conn.execute("INSERT INTO voter_assignments (national_id, user_id, assigned_at) VALUES ('A200002', ?, 'x')", (viewer,))
    conn.commit()
    page = a.get("/pledges").get_data(as_text=True)
    assert "<th>Agent</th>" in page and f'<option value="{viewer}" selected>viewer</option>' in page  # admins pick it
    rows = list(_csv.reader(io.StringIO(a.get("/pledges/export.csv").get_data(as_text=True).lstrip("\ufeff"))))
    assert {r[1]: r[rows[0].index("Agent")] for r in rows[1:]}["A200002"] == "viewer"


def test_connector_field_and_agent_dropdown(app):
    import csv as _csv

    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    token, ids = setup(a)
    mark(a, token, "A200001", ids["Shoppe"], "yes")
    r = a.post("/pledges/connector", data={"csrf_token": token, "national_id": "A200001", "connector": "  Ali   Moosa "},
               headers={"X-Requested-With": "fetch"})
    assert r.get_json() == {"ok": True, "connector": "Ali Moosa"}
    page = a.get("/pledges").get_data(as_text=True)
    assert 'value="Ali Moosa"' in page and "data-connector-form" in page
    assert 'value="Ali Moosa"' in a.get("/super").get_data(as_text=True)            # the same field on the super sheet
    rows = list(_csv.reader(io.StringIO(a.get("/pledges/export.csv").get_data(as_text=True).lstrip("\ufeff"))))
    col = {r[1]: r[rows[0].index("Connector")] for r in rows[1:]}
    assert col["A200001"] == "Ali Moosa"                                            # typed wins over who marked
    a.post("/pledges/connector", data={"csrf_token": token, "national_id": "A200001", "connector": ""})
    rows = list(_csv.reader(io.StringIO(a.get("/pledges/export.csv").get_data(as_text=True).lstrip("\ufeff"))))
    assert {r[1]: r[rows[0].index("Connector")] for r in rows[1:]}["A200001"] == "admin"  # cleared: back to who marked
    # the Agent drop-down on the pledge sheet saves through the agent sheet
    conn = connect(app.config["DATABASE"])
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    assert "data-agent-form" in page
    a.post("/agents/sheet/set", data={"csrf_token": token, "national_id": "A200002", "agent": str(viewer)})
    assert conn.execute("SELECT user_id FROM voter_assignments WHERE national_id = 'A200002'").fetchone()[0] == viewer
    # a view-only account sees text, can't save
    a.post(f"/admin/users/{viewer}", data={"csrf_token": token, "action": "set_pledge_access", "level": "view"})
    conn.execute("UPDATE users SET must_change_password = 0 WHERE id = ?", (viewer,))
    conn.commit()
    v = app.test_client()
    login(v, "viewer", USER_PW)
    vpage = v.get("/pledges").get_data(as_text=True)
    assert "<th>Connector</th>" in vpage and "data-connector-form" not in vpage and "data-agent-form" not in vpage
    assert v.post("/pledges/connector", data={"csrf_token": csrf(v, "/pledges"), "national_id": "A200001",
                                              "connector": "x"}).status_code == 403


def test_connector_filter(app):
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    token, ids = setup(a)
    mark(a, token, "A200001", ids["Shoppe"], "yes")
    mark(a, token, "A200002", ids["Shoppe"], "yes")
    a.post("/pledges/connector", data={"csrf_token": token, "national_id": "A200002", "connector": "Ali Moosa"})
    page = a.get("/pledges").get_data(as_text=True)
    assert '<select name="cn">' in page and ">Ali Moosa</option>" in page and ">admin</option>" in page
    rows = lambda q: a.get(f"/pledges/export.csv?{q}").get_data(as_text=True).strip().splitlines()[1:]
    ali = rows("cn=ali+moosa")
    assert len(ali) == 1 and "A200002" in ali[0]                     # the typed connector (any case)
    adm = rows("cn=admin")
    assert len(adm) == 1 and "A200001" in adm[0]                     # who marked, when nothing is typed
    assert all("A200001" not in r and "A200002" not in r for r in rows("cn=none"))
    assert "selected>Ali Moosa</option>" in a.get("/pledges?cn=ali+moosa").get_data(as_text=True)


def test_needs_transport_on_the_pledge_sheet(app):
    add_owner(app)
    a = app.test_client()
    login(a, "admin", OWNER_PW)
    token, ids = setup(a)
    assert "<th class=\"cand ride-col\">Transport</th>" in a.get("/pledges").get_data(as_text=True)
    go = lambda v: a.post("/pledges/transport", data={"csrf_token": token, "national_id": "A200001", "value": v},
                          headers={"X-Requested-With": "fetch"})
    assert go("1").get_json() == {"ok": True, "status": "waiting", "label": "Needs ride"}
    conn = connect(app.config["DATABASE"])
    row = conn.execute("SELECT status, source FROM war_transport WHERE national_id = 'A200001'").fetchone()
    assert tuple(row) == ("waiting", "pledge sheet")                                 # in the War Room queue
    import csv as _csv
    rows = list(_csv.reader(io.StringIO(a.get("/pledges/export.csv").get_data(as_text=True).lstrip("﻿"))))
    assert {r[1]: r[rows[0].index("Transport")] for r in rows[1:]}["A200001"] == "Needs ride"
    assert go("0").get_json()["status"] == ""                                       # taken back
    assert conn.execute("SELECT status FROM war_transport WHERE national_id = 'A200001'").fetchone()[0] == "cancelled"
    go("1")
    conn.execute("UPDATE war_transport SET status = 'onway' WHERE national_id = 'A200001'")
    conn.commit()
    r = go("0")
    assert r.status_code == 409 and "on the way" in r.get_json()["error"]           # a runner has it: War Room cancels
    assert ">On the way</span>" in a.get("/pledges").get_data(as_text=True)
    conn.execute("INSERT INTO war_voted (national_id, at, by_user, tier) VALUES ('A200002', 'x', 1, 'room')")
    conn.commit()
    r = a.post("/pledges/transport", data={"csrf_token": token, "national_id": "A200002", "value": "1"},
               headers={"X-Requested-With": "fetch"})
    assert r.status_code == 409 and "already voted" in r.get_json()["error"]
