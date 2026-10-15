"""'100% confirmed' voters: only the account named 'admin' can see or change them."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import hash_password  # noqa: E402
from app.db import connect, now_iso  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_campaign import set_parties  # noqa: E402
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402

OWNER_PW = "owner-pass-1234"


def add_owner(app):
    conn = connect(app.config["DATABASE"])
    stamp = now_iso(precise=True)
    conn.execute("INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at,"
                 " created_at) VALUES ('admin', ?, 'admin', 0, ?, ?)", (hash_password(OWNER_PW), stamp, stamp))
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]


def cards(client, path):
    return client.get(path).get_data(as_text=True).count('id="v-A2000')


def test_owner_marks_and_sees_metrics(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token = csrf(o, "/")
    page = o.get("/").get_data(as_text=True)
    assert page.count("Mark confirmed") == 3 and "100% confirmed" in page        # toggle + filter

    r = o.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "1"},
               headers={"X-Requested-With": "fetch"})
    assert r.json == {"ok": True, "confirmed": True, "changed": True,
                      "metrics": {"confirmed": 1, "total": 3, "pct": 33.3}}
    # same request again changes nothing (wanted state, not a toggle)
    r = o.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "1"},
               headers={"X-Requested-With": "fetch"})
    assert r.json["changed"] is False
    o.post("/voters/A200002/confirm", data={"csrf_token": token, "confirmed": "1", "note": "needs transport"})
    assert o.post("/voters/A200002/confirm", data={"csrf_token": token, "confirmed": "x"}).status_code == 400

    assert cards(o, "/?cf=1") == 2 and cards(o, "/?cf=0") == 1
    detail = o.get("/voters/A200002").get_data(as_text=True)
    assert "needs transport" in detail and "Remove confirmation" in detail
    m = o.get("/confirmed/metrics.json").json
    assert (m["confirmed"], m["total"], m["pct"]) == (2, 3, 66.7)
    assert "66.7%" in o.get("/confirmed").get_data(as_text=True)
    assert "100% confirmed" in o.get("/dashboard").get_data(as_text=True)

    lines = o.get("/export.csv?cf=1").get_data(as_text=True).strip().splitlines()
    assert len(lines) == 3 and lines[0].endswith("100% confirmed,Confirmed on") and ",yes," in lines[1]

    # the main number is out of the campaign's voters (e.g. MDP)
    set_parties(o, ["AAA"])
    m = o.get("/confirmed/metrics.json").json
    assert (m["label"], m["confirmed"], m["total"], m["pct"]) == ("AAA", 1, 1, 100.0)
    assert (m["all_confirmed"], m["all_total"]) == (2, 3)

    o.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "0"})
    assert o.get("/confirmed/metrics.json").json["all_confirmed"] == 1
    # deleting a voter drops their confirmation
    o.post("/admin/view-all", data={"csrf_token": token})
    o.post("/voters/A200002/delete", data={"csrf_token": token})
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM confirmed_voters").fetchone()[0] == 0
    assert "voter_confirm" in o.get("/admin/audit").get_data(as_text=True)


def test_nobody_else_can_see_or_change_it(app):
    owner_id = add_owner(app)
    add_agent(app, "ali")
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    o.post("/voters/A200001/confirm", data={"csrf_token": csrf(o, "/"), "confirmed": "1"})

    b = app.test_client()                      # another admin
    login(b, "boss", ADMIN_PW)
    u = app.test_client()
    login(u, "viewer", USER_PW)
    g = app.test_client()
    login(g, "ali", AGENT_PW)
    for c in (b, u, g):
        token = csrf(c, "/")
        assert c.post("/voters/A200001/confirm", data={"csrf_token": token, "confirmed": "0"}).status_code == 403
        assert c.get("/confirmed").status_code == 403
        assert c.get("/confirmed/metrics.json").status_code == 403
    page = b.get("/").get_data(as_text=True)
    assert "Mark confirmed" not in page and "100% confirmed" not in page
    assert cards(b, "/?cf=1") == 3                                   # the filter is ignored for others
    assert "100% confirmed" not in b.get("/export.csv?cf=1").get_data(as_text=True)
    assert "100% confirmed" not in b.get("/dashboard").get_data(as_text=True)
    assert "confirmed" not in b.get("/voters/A200001").get_data(as_text=True).lower().replace("not confirmed", "")
    # other admins don't see confirm entries in the activity log
    log = b.get("/admin/audit").get_data(as_text=True)
    assert "voter_confirm" not in log and "access_denied" in log
    assert connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM confirmed_voters").fetchone()[0] == 1

    # and can't take over the 'admin' account
    token = csrf(b, "/admin/users")
    assert "Protected: only admin" in b.get("/admin/users").get_data(as_text=True)
    for data in ({"action": "reset_password"}, {"action": "toggle_active"}, {"action": "set_role", "role": "user"}):
        b.post(f"/admin/users/{owner_id}", data={"csrf_token": token, **data})
    row = connect(app.config["DATABASE"]).execute(
        "SELECT role, is_active, must_change_password FROM users WHERE id = ?", (owner_id,)).fetchone()
    assert tuple(row) == ("admin", 1, 0)
    assert o.get("/confirmed").status_code == 200                    # owner's session still works


def test_a_differently_named_admin_is_not_the_owner(app):
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    assert a.get("/confirmed").status_code == 403
