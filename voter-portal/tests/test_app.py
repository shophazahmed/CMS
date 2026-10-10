import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.db import connect, now_iso  # noqa: E402

ADMIN_PW, USER_PW = "admin-pass-123", "user-pass-1234"


@pytest.fixture()
def app(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "test", "DATABASE": str(tmp_path / "t.db"),
                      "PHOTOS_DIR": str(tmp_path / "photos"), "IMPORTS_DIR": str(tmp_path / "imports"),
                      "SESSION_COOKIE_SECURE": False})
    conn = connect(app.config["DATABASE"])
    stamp = now_iso(precise=True)
    for name, role, pw in (("boss", "admin", ADMIN_PW), ("viewer", "user", USER_PW)):
        conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                     " created_at) VALUES (?, ?, ?, 0, ?, ?)", (name, hash_password(pw), role, stamp, stamp))
    for nid, name, sex, age, house, party in (
        ("A200001", "Aminath Alpha", "F", 30, "Rose Villa", "AAA"),
        ("A200002", "Moosa Beta", "M", 55, "Blue Ge", "BBB"),
        ("A200003", "=HYPERLINK(1)", "M", 70, "Rose Villa", ""),
    ):
        conn.execute("INSERT INTO voters (national_id, full_name, sex, age, house_name, party, created_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (nid, name, sex, age, house, party, stamp, stamp))
    conn.commit()
    conn.close()
    return app


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, username, password):
    return client.post("/login", data={"username": username, "password": password,
                                       "csrf_token": csrf(client)})


def test_requires_login(app):
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.get("/voters/A200001").status_code == 302
    assert c.get("/export.csv").status_code == 302


def test_csrf_enforced(app):
    c = app.test_client()
    c.get("/login")
    assert c.post("/login", data={"username": "boss", "password": ADMIN_PW}).status_code == 400


def test_user_is_read_only(app):
    c = app.test_client()
    assert login(c, "viewer", USER_PW).status_code == 302
    html = c.get("/?party=AAA").get_data(as_text=True)
    assert "Aminath Alpha" in html and "Moosa Beta" not in html
    assert "Export" not in html
    assert c.get("/?sex=M&age_min=50&age_max=60").get_data(as_text=True).count('id="v-A2000') == 1
    assert c.get("/?house=rose").get_data(as_text=True).count('id="v-A2000') == 2
    assert c.get("/?national_id=a200002").get_data(as_text=True).count('id="v-A2000') == 1
    assert c.get("/voters/A200001").status_code == 200
    token = csrf(c, "/")
    for method, path in (("get", "/export.csv"), ("get", "/export.xlsx"), ("get", "/voters/new"),
                         ("get", "/voters/A200001/edit"), ("post", "/voters/A200001/delete"),
                         ("get", "/admin/users"), ("get", "/admin/audit")):
        kwargs = {"data": {"csrf_token": token}} if method == "post" else {}
        assert getattr(c, method)(path, **kwargs).status_code == 403, path


def test_admin_crud_export_and_audit(app):
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    token = csrf(c, "/")
    r = c.post("/voters/new", data={"csrf_token": token, "national_id": "a 200009", "full_name": " New  Person ",
                                    "sex": "F", "age": "22", "house_name": "X", "party": "ccc"})
    assert r.status_code == 302
    page = c.get("/voters/A200009").get_data(as_text=True)
    assert "New Person" in page and "CCC" in page
    assert c.post("/voters/new", data={"csrf_token": token, "national_id": "A200009", "full_name": "Dup"}).status_code == 400
    r = c.post("/voters/A200009/edit", data={"csrf_token": token, "full_name": "New Person", "sex": "F",
                                             "age": "23", "house_name": "X", "party": "CCC"})
    assert r.status_code == 302
    csv_text = c.get("/export.csv?house=rose").get_data(as_text=True)
    assert csv_text.startswith("﻿ID,") and "'=HYPERLINK(1)" in csv_text and "Moosa" not in csv_text
    xlsx = c.get("/export.xlsx")
    assert xlsx.status_code == 200 and xlsx.data[:2] == b"PK"
    assert c.post("/voters/A200009/delete", data={"csrf_token": token}).status_code == 302
    assert c.get("/voters/A200009").status_code == 404
    audit = c.get("/admin/audit").get_data(as_text=True)
    for action in ("voter_create", "voter_update", "voter_delete", "export", "login"):
        assert action in audit


def test_user_management(app):
    c = app.test_client()
    login(c, "boss", ADMIN_PW)
    token = csrf(c, "/admin/users")
    r = c.post("/admin/users", data={"csrf_token": token, "username": "newbie", "role": "user", "password": ""},
               follow_redirects=True)
    temp = re.search(r"shown once\): (\S+)<", r.get_data(as_text=True)).group(1)
    # first sign-in forces a password change
    n = app.test_client()
    login(n, "newbie", temp)
    assert n.get("/").headers["Location"].endswith("/account/password")
    # deactivation ends the session
    conn = connect(app.config["DATABASE"])
    uid = conn.execute("SELECT id FROM users WHERE username = 'newbie'").fetchone()[0]
    c.post(f"/admin/users/{uid}", data={"csrf_token": token, "action": "toggle_active"})
    assert n.get("/account/password").status_code == 302
    # the only admin can't demote or deactivate themselves
    boss = conn.execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    c.post(f"/admin/users/{boss}", data={"csrf_token": token, "action": "set_role", "role": "user"})
    c.post(f"/admin/users/{boss}", data={"csrf_token": token, "action": "toggle_active"})
    assert tuple(conn.execute("SELECT role, is_active FROM users WHERE id = ?", (boss,)).fetchone()) == ("admin", 1)


def test_lockout(app):
    c = app.test_client()
    for _ in range(5):
        assert login(c, "viewer", "wrong-password").status_code == 401
    r = login(c, "viewer", USER_PW)
    assert r.status_code == 401 and "Too many failed attempts" in r.get_data(as_text=True)


def test_security_headers(app):
    r = app.test_client().get("/login")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_bootstrap_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("VOTER_PORTAL_ADMIN_PASSWORD", "first-admin-pass-1")
    cfg = {"TESTING": True, "SECRET_KEY": "t", "DATABASE": str(tmp_path / "b.db"), "PHOTOS_DIR": str(tmp_path / "p"),
           "IMPORTS_DIR": str(tmp_path / "i")}
    create_app(cfg)
    create_app(cfg)  # second start: no duplicate, no error
    conn = connect(cfg["DATABASE"])
    rows = conn.execute("SELECT username, role, must_change_password FROM users").fetchall()
    assert [tuple(r) for r in rows] == [("admin", "admin", 1)]


def test_phone_visible_searchable_editable(app):
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET phone = '7771234' WHERE national_id = 'A200001'")
    conn.commit()
    u = app.test_client()
    login(u, "viewer", USER_PW)
    page = u.get("/?q=777123").get_data(as_text=True)
    assert 'href="tel:7771234"' in page and page.count('id="v-A2000') == 1
    assert "7771234" in u.get("/voters/A200001").get_data(as_text=True)

    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/")
    a.post("/voters/A200002/edit", data={"csrf_token": token, "full_name": "Moosa Beta", "sex": "M", "age": "55",
                                         "house_name": "Blue Ge", "party": "BBB", "phone": "+960 912-3456"})
    assert conn.execute("SELECT phone FROM voters WHERE national_id = 'A200002'").fetchone()[0] == "9123456"
    assert "voter_update" in a.get("/admin/audit?target=A200002").get_data(as_text=True)
    assert "Phone" in a.get("/export.csv").get_data(as_text=True).splitlines()[0]


def test_canvassing_status_and_notes(app):
    u = app.test_client()
    login(u, "viewer", USER_PW)  # read-only users canvass too
    token = csrf(u, "/")
    page = u.get("/").get_data(as_text=True)
    assert "Not contacted <b data-count=\"new\">3</b>" in page

    # background save from a card
    r = u.post("/voters/A200001/canvass", data={"csrf_token": token, "status": "contacted"},
               headers={"X-Requested-With": "fetch"})
    assert r.get_json() == {"ok": True, "changed": True, "status": "contacted", "label": "Contacted", "by": "viewer"}
    # status + notes from the voter page (normal form post)
    r = u.post("/voters/A200002/canvass", data={"csrf_token": token, "status": "followup",
                                                "notes": " Wants a call after 5pm \r\n", "back": "/voters/A200002"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/voters/A200002")
    detail = u.get("/voters/A200002").get_data(as_text=True)
    assert "Wants a call after 5pm" in detail and "by viewer" in detail

    assert u.get("/?status=contacted").get_data(as_text=True).count('id="v-A2000') == 1
    page = u.get("/?status=followup").get_data(as_text=True)
    assert page.count('id="v-A2000') == 1 and "Wants a call after 5pm" in page
    assert u.get("/?q=after+5pm").get_data(as_text=True).count('id="v-A2000') == 1  # notes are searchable
    assert u.post("/voters/A200001/canvass", data={"csrf_token": token, "status": "bogus"}).status_code == 400
    # still read-only for everything else
    assert u.post("/voters/A200001/edit", data={"csrf_token": token, "full_name": "X"}).status_code == 403

    conn = connect(app.config["DATABASE"])
    row = conn.execute("SELECT status, notes, updated_by FROM voters WHERE national_id = 'A200002'").fetchone()
    assert tuple(row) == ("followup", "Wants a call after 5pm", None)  # canvassing doesn't count as an edit

    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    assert "voter_canvass" in a.get("/admin/audit").get_data(as_text=True)
    header = a.get("/export.csv").get_data(as_text=True).splitlines()[0]
    assert header.endswith("Status,Status changed,Notes")
