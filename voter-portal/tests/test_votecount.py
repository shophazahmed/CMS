import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, app, csrf, login  # noqa: E402,F401
from tests.test_warroom import make, setup  # noqa: E402


def tap(c, token, combo=None, undo=False, key=None):
    data = {"csrf_token": token}
    if undo:
        data["undo"] = "1"
    else:
        data["combo"] = combo
    if key:
        data["key"] = key
    return c.post("/count/tap", data=data, headers={"X-Requested-With": "fetch"})


def test_tally_final_and_results(app):
    from PIL import Image

    o, token, ids = setup(app)                         # Rayaan #1, Sofhaz #2 (ours), Jawid #3, Thalaal #4
    o.post("/war/team", data={"csrf_token": token, "action": "open"})
    t1, _ = make(app, o, token, "boxagent", "t1", "Box A")
    s, r, j = ids["Sofhaz"], ids["Rayaan"], ids["Jawid"]
    page = t1.get("/count").get_data(as_text=True)
    lo, hi = sorted((r, s))
    assert "Vote count · Box A" in page and f'data-combo="p:{r}-{s}"' in page and 'data-combo="x"' in page
    tok = csrf(t1, "/count")
    assert tap(t1, tok, f"p:{r}-{s}", key="a").json["ballots"] == 1
    assert tap(t1, tok, f"p:{r}-{s}", key="a").json["repeat"]          # resent after a signal drop: once
    tap(t1, tok, f"p:{s}-{j}")
    tap(t1, tok, f"s:{s}")
    tap(t1, tok, "x")
    d = tap(t1, tok, f"s:{j}").json
    assert d["ballots"] == 5
    d = tap(t1, tok, undo=True, key="u1").json                         # the Jawid-only tap is taken back
    assert d["ballots"] == 4 and d["votes"] == {str(r): 1, str(s): 3, str(j): 1}
    assert tap(t1, tok, undo=True, key="u1").json["repeat"]
    assert tap(t1, tok, "bogus").status_code == 400
    # the official sheet, with a photo; one number differs
    img = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(img, "PNG")
    img.seek(0)
    data = {"csrf_token": tok, f"c{r}": "1", f"c{s}": "3", f"c{j}": "2", f"c{ids['Thalaal']}": "0", "invalid": "1",
            "ballots": "4", "photo": (img, "sheet.png")}
    page = t1.post("/count/final", data=data, content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
    assert "differ" in page and "count-diff" in page and "Photo of the sheet" in page
    assert tap(t1, tok, "x").status_code == 409                       # final: no more taps
    csv_text = t1.get("/count/export.csv").get_data(as_text=True)
    assert "Vote count · Box A · final" in csv_text and "Official sheet" in csv_text and "Invalid / blank,1,1,0" in csv_text
    assert t1.get("/count/export.csv?box=Box+B").get_data(as_text=True).count("Box A") >= 1   # always their own box
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(t1.get("/count/export.xlsx").data)).active
    assert ws["A1"].value.startswith("Vote count · Box A")
    assert "Vote count · Box B" in o.get("/count/export.csv?box=Box+B").get_data(as_text=True)
    assert t1.get("/count/photo/Box A").status_code == 200
    assert t1.get("/count/photo/Box B").status_code == 403
    assert t1.get("/count/results").status_code in (302, 403)         # results: War Room only
    # results for the owner: the official numbers count for a final box
    res = " ".join(o.get("/count/results").get_data(as_text=True).split())
    assert "<strong>1</strong> of 2 boxes final" in res and "Wins · " in res and "sheet ≠ tally" in res
    assert "Who our voters paired us with" in res
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(o.get("/count/results.xlsx").data))
    assert wb.sheetnames == ["Results", "By box", "Voting pattern"]
    # the owner can reopen
    o.post("/count/reopen", data={"csrf_token": token, "box": "Box A"})
    assert tap(t1, tok, "x").status_code == 200
    # a box agent works only while the War Room is open
    o.post("/war/team", data={"csrf_token": token, "action": "close"})
    assert t1.get("/count").status_code == 423
    b = app.test_client()
    login(b, "boss", ADMIN_PW)
    assert b.get("/count/results").status_code == 403
