import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401


def official_xlsx(rows, code="LOC-GDH-TEST", box_parts=("Gdh.", "Test", "Box"), voters=None):
    """A sheet laid out like the Elections Commission list (two 'pages')."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Island list"])
    ws.append(["", "", "", "", "PUBLIC"])
    ws.append(["", "BALLOT BOX"])
    ws.append(["", code])
    ws.append(["", *box_parts])
    ws.append(["", f"{voters if voters is not None else len(rows)} voters"])
    half = (len(rows) + 1) // 2
    for chunk in (rows[:half], rows[half:]):
        ws.append(["Page"])
        ws.append(["No.", "Name", "Island", "House", "", "Constituency", "AR", "", "YW"])
        for no, name, house in chunk:
            ws.append([no, name, "GDh. Gadhdhoo", house, "", "Q05", "", "", ""])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def test_official_lists(app):
    conn = connect(app.config["DATABASE"])
    v = {r["national_id"]: r for r in conn.execute("SELECT national_id, full_name, house_name FROM voters")}
    a1, a2 = v["A200001"], v["A200002"]
    rows = [(1, a1["full_name"], a1["house_name"]),                       # name and house
            (2, a2["full_name"], "Somewhere Else"),                       # name only
            (3, "Nobody Known", "Nowhere")]                               # not matched
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    tok = csrf(b, "/war/official")
    page = b.post("/war/official", data={"csrf_token": tok, "apply": "0", "set_box": "1",
                                         "lists": (official_xlsx(rows, voters=4), "LOC-GDH-TEST.xlsx")},
                  content_type="multipart/form-data").get_data(as_text=True)
    assert "Check (nothing saved yet)" in page and "Gdh. Test Box" in page and "LOC-GDH-TEST" in page
    assert "Not matched (1)" in page and "Nobody Known" in page and "Some pages are missing" in page
    assert conn.execute("SELECT COUNT(*) FROM war_list").fetchone()[0] == 0           # check only
    assert "Nobody Known" in b.get("/war/official/unmatched.csv").get_data(as_text=True)
    page = b.post("/war/official", data={"csrf_token": tok, "apply": "1", "set_box": "1",
                                         "lists": (official_xlsx(rows), "LOC-GDH-TEST.xlsx")},
                  content_type="multipart/form-data").get_data(as_text=True)
    assert "Saved" in page
    got = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT national_id, list_no, box FROM war_list")}
    assert got == {"A200001": (1, "Gdh. Test Box"), "A200002": (2, "Gdh. Test Box")}
    assert conn.execute("SELECT location FROM voters WHERE national_id = 'A200001'").fetchone()[0] == "Gdh. Test Box"
    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert u.get("/war/official").status_code == 403
