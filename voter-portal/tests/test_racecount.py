import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, app, csrf, login  # noqa: E402,F401
from tests.test_super import owner  # noqa: E402

PW = "atoll-pass-12345"


def setup(app):
    o, token, ids = owner(app)
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Box A'")
    conn.commit()
    o.post("/atoll/manage", data={"csrf_token": token, "action": "add_race", "name": "GDh - Atoll GM Member",
                                  "seats": "2", "max_votes": "2"})
    rid = conn.execute("SELECT id FROM races").fetchone()[0]
    for no, name, place in ((1, "Candidate One", "Gadhdhoo, House 1"), (2, "Candidate Two", "Island B, House 2"),
                            (4, "Candidate Four", "Island B, House 4"), (6, "Candidate Six", "Island C, House 6")):
        o.post("/atoll/manage", data={"csrf_token": token, "action": "add_cand", "race_id": rid, "ballot_no": no,
                                      "name": name, "place": place})
    cids = [r[0] for r in conn.execute("SELECT id FROM race_candidates ORDER BY ballot_no")]
    return o, token, conn, rid, cids


def person(app, o, token, conn, name, role, rid, box=""):
    o.post("/admin/users", data={"csrf_token": token, "username": name, "role": "user", "password": PW})
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]
    o.post(f"/admin/users/{uid}", data={"csrf_token": token, "action": "set_race", "race_role": role, "race_id": rid,
                                        "race_box": box})
    c = app.test_client()
    login(c, name, PW)
    return c, uid


def test_atoll_count_flow_and_access(app):
    o, token, conn, rid, (c1, c2, c4, c6) = setup(app)
    counter, _ = person(app, o, token, conn, "atollbox", "count", rid, "Box A")
    viewer, _ = person(app, o, token, conn, "atollview", "watch", rid)
    # the box counter lands on their count and reaches nothing else
    assert counter.get("/").headers["Location"].endswith("/atoll")
    assert counter.get("/atoll").headers["Location"].endswith(f"/atoll/{rid}/count")
    page = counter.get(f"/atoll/{rid}/count").get_data(as_text=True)
    assert "Count · Box A" in page and "Candidate Four" in page and "Island B, House 4" in page
    for path in ("/pledges", "/war", "/admin/users", "/voters/A200001", "/count", f"/atoll/{rid}/results"):
        r = counter.get(path)
        assert r.status_code in (302, 403), path
    tok = csrf(counter, f"/atoll/{rid}/count")
    tap = lambda combo, key: counter.post(f"/atoll/{rid}/tap", data={"csrf_token": tok, "combo": combo, "key": key},
                                          headers={"X-Requested-With": "fetch"})
    assert tap(f"p:{c1}-{c4}", "k1").json["ballots"] == 1
    assert tap(f"p:{c1}-{c4}", "k1").json["repeat"]                          # resent: counted once
    tap(f"s:{c2}", "k2")
    tap("x", "k3")
    d = counter.post(f"/atoll/{rid}/tap", data={"csrf_token": tok, "undo": "1", "key": "u1"},
                     headers={"X-Requested-With": "fetch"}).json
    assert d["ballots"] == 2 and d["votes"] == {str(c1): 1, str(c2): 1, str(c4): 1}   # the invalid one taken back
    assert tap("p:999-998", "bad").status_code == 400
    # the island council count is untouched
    assert conn.execute("SELECT COUNT(*) FROM count_tally").fetchone()[0] == 0
    # the results viewer: totals and every box, nothing else
    res = viewer.get(f"/atoll/{rid}/results").get_data(as_text=True)
    assert "GDh - Atoll GM Member" in res and "Candidate One" in res and "Box A" in res
    assert viewer.get("/war").status_code in (302, 403) and viewer.get("/pledges").status_code in (302, 403)
    assert viewer.get(f"/atoll/{rid}/results.xlsx").status_code == 200
    # official sheet makes the box final
    r = counter.post(f"/atoll/{rid}/final", data={"csrf_token": tok, f"c{c1}": "1", f"c{c2}": "0", f"c{c4}": "1",
                                                  f"c{c6}": "0", "invalid": "0", "ballots": "1"}, follow_redirects=True)
    assert "Box final" in r.get_data(as_text=True)
    assert tap("x", "k9").status_code == 409
    # the owner sees it in the menu; an ordinary user doesn't
    assert "/atoll" in o.get("/").get_data(as_text=True)
    assert o.get(f"/atoll/{rid}/results").status_code == 200
    b = app.test_client()
    login(b, "viewer", "user-pass-1234")
    assert b.get(f"/atoll/{rid}/results").status_code == 403
    # access can't go to admins
    boss = conn.execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    o.post(f"/admin/users/{boss}", data={"csrf_token": token, "action": "set_race", "race_role": "watch", "race_id": rid})
    assert conn.execute("SELECT race_role FROM users WHERE id = ?", (boss,)).fetchone()[0] == ""


def test_atoll_count_on_the_public_page(app):
    import io as _io

    from PIL import Image

    o, token, conn, rid, (c1, c2, c4, c6) = setup(app)
    img = _io.BytesIO()
    Image.new("RGB", (40, 50), "green").save(img, "PNG")
    img.seek(0)
    o.post("/atoll/manage", data={"csrf_token": token, "action": "photo", "cand_id": c1, "photo": (img, "a.png")},
           content_type="multipart/form-data")
    o.post("/war/team", data={"csrf_token": token, "action": "public_link"})
    code = conn.execute("SELECT value FROM settings WHERE key = 'public_board_token'").fetchone()[0]
    anyone = app.test_client()
    turnout = anyone.get(f"/live/{code}").get_data(as_text=True)
    assert f"/live/{code}/race/{rid}" in turnout and "GDh - Atoll GM Member" in turnout       # its tab
    page = anyone.get(f"/live/{code}/race/{rid}").get_data(as_text=True)
    assert "Candidate One" in page and "Gadhdhoo, House 1" in page and "2 seats" in page
    assert f"/live/{code}/race/{rid}/candidate/{c1}.jpg" in page
    assert anyone.get(f"/live/{code}/race/{rid}/candidate/{c1}.jpg").mimetype == "image/jpeg"
    assert anyone.get(f"/live/wrong/race/{rid}").status_code == 404
    o.post("/war/team", data={"csrf_token": token, "action": "public_count", "on": "0"})      # hidden with the count
    assert anyone.get(f"/live/{code}/race/{rid}").status_code == 404
