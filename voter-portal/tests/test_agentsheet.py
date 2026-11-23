import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401


def test_assign_agents_sheet(app):
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    conn = connect(app.config["DATABASE"])
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    boss = conn.execute("SELECT id FROM users WHERE username = 'boss'").fetchone()[0]
    house = conn.execute("SELECT house_name FROM voters WHERE national_id = 'A200001'").fetchone()[0]
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES (?, ?, '2026-01-01')", (house, boss))
    conn.commit()
    page = b.get("/agents/sheet").get_data(as_text=True)
    assert "Assign agents" in page and "Aminath Alpha" in page and "Moosa Beta" in page and "House agent: boss" in page
    token = csrf(b, "/agents/sheet")
    r = b.post("/agents/sheet/set", data={"csrf_token": token, "national_id": "A200002", "agent": viewer},
               headers={"X-Requested-With": "fetch"})
    assert r.json == {"ok": True, "agent": "viewer"}
    assert conn.execute("SELECT user_id FROM voter_assignments WHERE national_id = 'A200002'").fetchone()[0] == viewer
    only = b.get(f"/agents/sheet?ag={viewer}").get_data(as_text=True)
    assert "Moosa Beta" in only and "Aminath Alpha</span>" not in only
    assert "Moosa Beta</span>" not in b.get(f"/agents/sheet?ag={boss}").get_data(as_text=True)
    # back to the house agent
    b.post("/agents/sheet/set", data={"csrf_token": token, "national_id": "A200002", "agent": ""}, headers={"X-Requested-With": "fetch"})
    assert conn.execute("SELECT COUNT(*) FROM voter_assignments").fetchone()[0] == 0
    # many at once: ticked, then everyone matching the filters
    b.post("/agents/sheet/assign", data={"csrf_token": token, "agent": viewer, "scope": "ticked", "ids": ["A200001", "A200003"]})
    assert conn.execute("SELECT COUNT(*) FROM voter_assignments WHERE user_id = ?", (viewer,)).fetchone()[0] == 2
    b.post("/agents/sheet/assign?sex=M", data={"csrf_token": token, "agent": boss, "scope": "all"})
    assert {r[0] for r in conn.execute("SELECT national_id FROM voter_assignments WHERE user_id = ?", (boss,))} == {"A200002", "A200003"}
    assert "No agent" in b.get("/agents/sheet?ag=none").get_data(as_text=True)
    # admins only
    v = app.test_client()
    login(v, "viewer", USER_PW)
    assert v.get("/agents/sheet").status_code in (302, 403)


def test_assign_agents_export(app):
    import csv
    import io

    from openpyxl import load_workbook

    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    conn = connect(app.config["DATABASE"])
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    token = csrf(b, "/agents/sheet")
    b.post("/agents/sheet/set", data={"csrf_token": token, "national_id": "A200002", "agent": viewer},
           headers={"X-Requested-With": "fetch"})
    assert "/agents/sheet/export.xlsx" in b.get("/agents/sheet").get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(b.get(f"/agents/sheet/export.csv?ag={viewer}").get_data(as_text=True).lstrip("﻿"))))
    assert rows[0][8:] == ["Agent", "Given by", "House agent"]
    assert len(rows) == 2 and rows[1][1] == "A200002" and rows[1][8:10] == ["viewer", "One by one"]
    wb = load_workbook(io.BytesIO(b.get("/agents/sheet/export.xlsx").data))
    assert wb.active.max_row == 4
    v = app.test_client()
    login(v, "viewer", USER_PW)
    assert v.get("/agents/sheet/export.csv").status_code in (302, 403)
