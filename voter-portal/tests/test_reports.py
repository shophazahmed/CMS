import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_confirmed import OWNER_PW, add_owner  # noqa: E402
from tests.test_pledges import mark, setup  # noqa: E402


def rows_of(resp):
    return list(csv.reader(io.StringIO(resp.get_data(as_text=True).lstrip("﻿"))))


def test_default_and_custom_reports(app):
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    page = b.get("/reports").get_data(as_text=True)
    assert "Default reports" in page and "Voters by box and sex" in page and "Build a report" in page
    assert "Yes pledges by box" not in page                       # no pledge access, no pledge reports
    view = " ".join(b.get("/reports/view?r=box-sex").get_data(as_text=True).split())
    assert "Voters by box and sex" in view and "Men" in view and "Women" in view
    t = rows_of(b.get("/reports/export.csv?r=box-sex"))
    assert t[0][0] == "Box" and t[-1][0] == "Total" and t[-1][-1] == "3"      # 3 voters in the test data
    # custom: sex by age group, row %
    t = rows_of(b.get("/reports/export.csv?rows=sex&cols=age&pct=1"))
    assert t[0][0] == "Sex" and "100" in t[1][-1]
    # filters narrow it (the pledge sheet filters)
    assert rows_of(b.get("/reports/export.csv?rows=sex&sex=F"))[-1][-1] == "1"
    # several at once
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(b.get("/reports/export.xlsx?many=box-sex&many=status-box&many=agents").data))
    assert wb.sheetnames == ["Voters by box and sex", "Canvassing by box", "Voters per agent"]
    assert b.get("/reports/view?rows=bogus").status_code == 400
    v = app.test_client()
    login(v, "viewer", USER_PW)
    assert v.get("/reports").status_code in (302, 403)


def test_pledge_reports_for_the_owner(app):
    add_owner(app)
    o = app.test_client()
    login(o, "admin", OWNER_PW)
    token, ids = setup(o)
    mark(o, token, "A200001", ids["Shoppe"], "yes")
    mark(o, token, "A200001", ids["Rayya"], "yes")
    mark(o, token, "A200002", ids["Shoppe"], "yes")
    assert "Yes pledges by box" in o.get("/reports").get_data(as_text=True)
    t = rows_of(o.get("/reports/export.csv?rows=yes"))
    counts = {r[0]: r[1] for r in t[1:-1]}
    assert counts["Shoppe"] == "2" and counts["Rayya"] == "1" and counts["No Yes"] == "1"
    t = rows_of(o.get(f"/reports/export.csv?rows=p{ids['Shoppe']}"))
    assert {r[0]: r[1] for r in t[1:-1]} == {"Yes": "2", "Not marked": "1"}
    assert "Contact before the vote, by agent" in o.get("/reports").get_data(as_text=True)   # the owner sees War Room ones


def test_list_reports(app):
    from tests.test_super import owner, vote

    o, token, ids = owner(app)
    conn = connect(app.config["DATABASE"])
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    conn.execute("INSERT INTO voter_assignments (national_id, user_id, assigned_at) VALUES ('A200001', ?, 'x')", (viewer,))
    conn.commit()
    vote(o, token, "A200001", ids["Sofhaz"], "1")
    vote(o, token, "A200001", ids["Jawid"], "1")
    page = o.get("/reports").get_data(as_text=True)
    assert "Voters and their agent" in page and "Super pledge voters and their agent" in page
    t = rows_of(o.get("/reports/list/voters-agents/export.csv"))
    assert t[0][-2:] == ["Agent", "Given by"] and len(t) == 4
    mine = {r[1]: r for r in t[1:]}
    assert mine["A200001"][-2:] == ["viewer", "One by one"] and mine["A200002"][-2] == "No agent"
    t = rows_of(o.get("/reports/list/super-agents/export.csv"))
    assert len(t) == 2 and t[1][1] == "A200001" and t[1][8] == "Jawid, Sofhaz" and t[1][9] == "viewer"
    assert "viewer" in o.get("/reports/list/super-agents").get_data(as_text=True)
    # the agent filter: on the page and in the downloads
    page = o.get(f"/reports/list/voters-agents?ag={viewer}").get_data(as_text=True)
    assert 'name="ag"' in page and f'value="{viewer}" selected' in page
    t = rows_of(o.get(f"/reports/list/voters-agents/export.csv?ag={viewer}"))
    assert [r[1] for r in t[1:]] == ["A200001"]
    assert len(rows_of(o.get("/reports/list/voters-agents/export.csv?ag=none"))) == 3
    conn.execute("UPDATE voters SET location = 'Box A' WHERE national_id = 'A200002'")
    conn.commit()
    page = o.get("/reports/list/voters-agents?loc=Box+A").get_data(as_text=True)
    assert 'name="loc"' in page and 'selected>Box A<' in page and "<strong>1</strong> voter" in " ".join(page.split())
    assert [r[1] for r in rows_of(o.get("/reports/list/voters-agents/export.csv?loc=Box+A"))[1:]] == ["A200002"]
    b = app.test_client()
    login(b, "boss", ADMIN_PW)                                   # no super pledge access
    assert b.get("/reports/list/super-agents").status_code == 404
    assert b.get("/reports/list/voters-agents").status_code == 200


def test_candidate_reports_and_filters(app):
    from tests.test_super import owner, vote

    o, token, ids = owner(app)
    s, r = ids["Sofhaz"], ids["Rayaan"]
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Box A'")
    conn.execute("UPDATE voters SET location = 'Box B' WHERE national_id = 'A200003'")
    viewer = conn.execute("SELECT id FROM users WHERE username = 'viewer'").fetchone()[0]
    conn.execute("INSERT INTO voter_assignments (national_id, user_id, assigned_at) VALUES ('A200001', ?, 'x')", (viewer,))
    conn.commit()
    mark(o, token, "A200001", s, "yes")
    mark(o, token, "A200001", r, "yes")                       # shared
    mark(o, token, "A200002", s, "yes")                       # Sofhaz only
    mark(o, token, "A200003", r, "maybe")
    vote(o, token, "A200002", s, "1")
    page = o.get("/reports").get_data(as_text=True)
    assert "Candidates overview" in page and "One candidate" in page and 'name="loc"' in page
    t = rows_of(o.get("/reports/candidates/export.csv"))
    soph = next(x for x in t if x[0].endswith("Sofhaz"))
    # Yes, Maybe, No, Not marked, Solid, Shared, Expected, Super
    assert soph[1:] == ["2", "0", "0", "1", "1", "1", "2", "1"]
    # filtered by box
    soph = next(x for x in rows_of(o.get("/reports/candidates/export.csv?loc=Box+B")) if x[0].endswith("Sofhaz"))
    assert soph[1] == "0" and soph[4] == "1"
    # one candidate's report
    view = o.get(f"/reports/view?cand={s}&what=p-box").get_data(as_text=True)
    assert "Sofhaz: pledges by box" in view
    t = rows_of(o.get(f"/reports/export.csv?cand={s}&what=p-box"))
    assert t[0][:2] == ["Box", "Yes"] and t[1][0] == "Box A" and t[1][1] == "2"
    t = rows_of(o.get(f"/reports/export.csv?cand={s}&what=s-agent"))
    assert {x[0]: x[1] for x in t[1:-1]}["No agent"] == "1"           # the super voter has no agent
    # new dimensions
    t = rows_of(o.get("/reports/export.csv?rows=pattern"))
    got = {x[0]: x[1] for x in t[1:-1]}
    shared = [k for k in got if k.startswith("Shared: ")]
    assert got["Only Sofhaz"] == "1" and got["No Yes"] == "1" and len(shared) == 1 and got[shared[0]] == "1"
    t = rows_of(o.get("/reports/export.csv?rows=super"))
    assert {x[0]: x[1] for x in t[1:-1]} == {"Sofhaz": "1", "Not a super voter": "2"}
    # agent filter on a default report
    t = rows_of(o.get(f"/reports/export.csv?r=box-sex&ag={viewer}"))
    assert t[-1][-1] == "1"
    assert o.get(f"/reports/view?cand={s}&what=bogus").status_code == 400
