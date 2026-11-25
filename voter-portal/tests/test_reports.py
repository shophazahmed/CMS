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
