import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, app, csrf, login  # noqa: E402,F401
from tests.test_super import owner  # noqa: E402


def sheet(rows, headers=("Rayan", "Sofhaz", "Javiz", "Thalaal")):
    """Laid out like the box's ballot entry workbook: title lines, a header row, one row per ballot."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Ballot Entry"
    ws.append(["Gaumee Majilees • Box A"])
    ws.append(["Enter 1 under each selected candidate."])
    ws.append(["Ballot #", *headers, "Ballot status", "Voting pattern"])
    for n, (marks, status) in enumerate(rows, 1):
        ws.append([n] + [1 if k in marks else None for k in range(4)] + [status, ""])
    ws.append(["Ballot #", *headers, "Ballot status", "Voting pattern"])     # a repeated header row
    ws.append([99, None, None, None, None, None, None])                        # an empty ballot row
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def test_replace_a_box_count_from_excel(app):
    o, token, ids = owner(app)
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET location = 'Box A'")
    conn.commit()
    r_, s_, j_, t_ = ids["Rayaan"], ids["Sofhaz"], ids["Jawid"], ids["Thalaal"]
    # a wrong count at the box
    tok = csrf(o, "/count?box=Box+A")
    for n in range(3):
        o.post("/count/tap", data={"csrf_token": tok, "combo": f"s:{t_}", "key": f"w{n}", "box": "Box A"},
               headers={"X-Requested-With": "fetch"})
    rows = [((0, 1), None), ((0, 1), None), ((3,), None), ((0, 2), None), ((), "Blank")]
    page = " ".join(o.post("/count/import", data={"csrf_token": token, "box": "Box A", "apply": "0",
                                                  "sheet": (sheet(rows), "Box_A_Vote_Count.xlsm")},
                           content_type="multipart/form-data").get_data(as_text=True).split())
    assert "Check (nothing saved yet)" in page and "<strong>5</strong> ballots (1 blank or rejected)" in page
    assert f'<option value="{r_}" selected>' in page and f'<option value="{j_}" selected>' in page   # Rayan, Javiz matched
    assert conn.execute("SELECT COUNT(*) FROM count_tally WHERE voided_at IS NULL").fetchone()[0] == 3   # nothing saved
    o.post("/count/import", data={"csrf_token": token, "box": "Box A", "apply": "1",
                                  "sheet": (sheet(rows), "Box_A_Vote_Count.xlsm")}, content_type="multipart/form-data")
    live = conn.execute("SELECT combo, COUNT(*) FROM count_tally WHERE box = 'Box A' AND voided_at IS NULL"
                        " GROUP BY combo").fetchall()
    assert dict(live) == {f"p:{r_}-{s_}": 2, f"s:{t_}": 1, f"p:{r_}-{j_}": 1, "x": 1}
    assert conn.execute("SELECT COUNT(*) FROM count_tally WHERE voided_at IS NOT NULL").fetchone()[0] == 3   # old kept, undone
    res = o.get("/count/results").get_data(as_text=True)
    assert "Replace a box&#39;s count from Excel" in res or "Replace a box's count from Excel" in res
    # too many candidates on a ballot: refused, nothing saved
    bad = [((0, 1, 2), None)]
    page = o.post("/count/import", data={"csrf_token": token, "box": "Box A", "apply": "1",
                                         "sheet": (sheet(bad), "bad.xlsx")}, content_type="multipart/form-data").get_data(as_text=True)
    assert "has 3 candidates" in page
    assert conn.execute("SELECT COUNT(*) FROM count_tally WHERE box = 'Box A' AND voided_at IS NULL").fetchone()[0] == 5
    # not for others
    b = app.test_client()
    login(b, "viewer", "user-pass-1234")
    assert b.get("/count/import").status_code == 403
