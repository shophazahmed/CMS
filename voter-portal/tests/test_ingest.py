import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.normalize import normalize_age, normalize_id, normalize_sex  # noqa: E402
from scripts import ingest  # noqa: E402
from tests.fixtures import build  # noqa: E402


@pytest.mark.parametrize("raw, expected", [
    ("A100001", "A100001"), (" a 100001 ", "A100001"), ("A-100001", "A100001"),
    (100001, "A100001"), ("100001.0", "A100001"), ("A12345", "A012345"), ("", None), ("hello", None),
])
def test_normalize_id(raw, expected):
    assert normalize_id(raw)[0] == expected


def test_normalize_sex_and_age():
    assert normalize_sex("female")[0] == "F"
    assert normalize_sex("ފިރިހެން")[0] == "M"
    assert normalize_sex("x") == ("", "unrecognised sex value 'x'")
    assert normalize_age("45 yrs")[0] == 45
    assert normalize_age(200)[0] is None


@pytest.fixture()
def run(tmp_path):
    data = build(tmp_path / "data")
    db, log = tmp_path / "vp.db", tmp_path / "merge_conflicts.log"
    out = tmp_path / "merged.csv"
    assert ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "photos"),
                        "--log", str(log), "--csv-out", str(out)]) == 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    voters = {r["national_id"]: dict(r) for r in conn.execute("SELECT * FROM voters")}
    return voters, log.read_text(encoding="utf-8"), conn, tmp_path


def test_dedup_and_priority(run):
    voters, log, conn, _ = run
    assert sorted(voters) == [f"A10000{i}" for i in range(1, 10)]
    # 2026 list wins over new voters (age 34 vs 33), new voters' blank party doesn't erase AAA
    assert voters["A100001"]["age"] == 34 and voters["A100001"]["party"] == "AAA"
    # for the party, the updated party list beats even the 2026 list
    assert voters["A100002"]["party"] == "CCC"
    # "Updated Party" column is preferred over "Party", and the party list beats the old list
    assert voters["A100005"]["house_name"] == "Old House" and voters["A100005"]["party"] == "AAA"
    # cleaned values
    assert voters["A100004"]["full_name"] == "Hassan Demo"
    assert voters["A100004"]["party"] == "CCC" and voters["A100004"]["sex"] == "M"
    assert voters["A100003"]["sex"] == "F"


def test_photos(run):
    voters, _, _, tmp = run
    assert voters["A100001"]["photo_path"] == "A100001.jpg"   # embedded picture
    assert voters["A100002"]["photo_path"] == "A100002.jpg"   # embedded picture
    assert voters["A100004"]["photo_path"] == "A100004.jpg"   # Windows path in a column
    assert voters["A100009"]["photo_path"] == "A100009.jpg"   # file named after the ID
    assert voters["A100003"]["photo_path"] is None
    assert (tmp / "photos" / "A100001.jpg").is_file()


def test_conflict_log(run):
    _, log, _, _ = run
    for code in ("AMBIGUOUS_NAME_MISMATCH", "PARTY_CHANGED", "HOUSE_CHANGED", "DUPLICATE_IN_FILE",
                 "POSSIBLE_DUPLICATE_PERSON", "UNDER_18", "NO_VALID_ID", "ID_NORMALISED"):
        assert code in log, code
    assert "AGE_CONFLICT               A100006" in log   # 60 vs 30
    assert "AGE_CONFLICT               A100001" not in log  # 34 vs 33 is within a year


def test_seeded_users_and_rerun(run):
    _, _, conn, tmp = run
    users = {r["username"]: r["role"] for r in conn.execute("SELECT username, role FROM users")}
    assert users == {"admin": "admin", "user": "user"}
    # a voter edited in the portal survives a re-import
    conn.execute("UPDATE voters SET party = 'EDITED', updated_by = 1 WHERE national_id = 'A100002'")
    conn.commit()
    ingest.main(["--data-dir", str(tmp / "data"), "--db", str(tmp / "vp.db"),
                 "--photos-dir", str(tmp / "photos"), "--log", str(tmp / "log2")])
    assert conn.execute("SELECT party FROM voters WHERE national_id = 'A100002'").fetchone()[0] == "EDITED"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2


def test_mdp_list_membership(tmp_path):
    """Every voter on the MDP list is MDP; MDP voters missing from it have left MDP."""
    import csv
    from openpyxl import Workbook

    data = tmp_path / "data"
    data.mkdir()
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Name", "Sex", "Age", "House", "Party"])
    ws.append(["A400001", "Stays Mdp", "M", 40, "One", "MDP"])
    ws.append(["A400002", "Left Mdp", "F", 41, "Two", "M.D.P"])
    ws.append(["A400003", "Was Pnc", "M", 42, "Three", "PNC"])
    ws.append(["A400004", "Joins Mdp", "F", 43, "Four", "PNC"])
    ws.append(["A400005", "No Party", "M", 44, "Five", ""])
    wb.save(data / "final_cleaned_gadhdhoo_updated_party.xlsx")
    with open(data / "Gadhdhoo_updated_Party_list_MPD_.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["#", "National ID", "Name", "Phone", "House", "Atoll", "Island"])
        w.writerow([1, "A400001", "Stays Mdp", "", "One", "GDh", "Gadhdhoo"])
        w.writerow([2, "A400004", "Joins Mdp", "", "Four", "GDh", "Gadhdhoo"])
        w.writerow([3, "A400006", "Only On List", "", "Six", "GDh", "Gadhdhoo"])
    db, log = tmp_path / "vp.db", tmp_path / "log"
    ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "p"),
                 "--log", str(log), "--no-seed-users"])
    conn = sqlite3.connect(db)
    party = dict(conn.execute("SELECT national_id, party FROM voters"))
    assert party == {"A400001": "MDP", "A400002": "OTHER", "A400003": "PNC",
                     "A400004": "MDP", "A400005": "", "A400006": "MDP"}
    text = log.read_text()
    assert "PARTY_LEFT_MDP             A400002" in text and "A400003" not in text.split("PARTY_LEFT_MDP")[-1]
    assert "3 MDP (on the updated MDP list), 1 moved from MDP to OTHER" in text


def test_no_mdp_list_changes_nobody(tmp_path):
    from openpyxl import Workbook

    data = tmp_path / "data"
    data.mkdir()
    wb = Workbook()
    ws = wb.active
    ws.append(["ID", "Name", "Party"])
    ws.append(["A400001", "Stays Mdp", "MDP"])
    wb.save(data / "final_cleaned_gadhdhoo_updated_party.xlsx")
    db = tmp_path / "vp.db"
    ingest.main(["--data-dir", str(data), "--db", str(db), "--photos-dir", str(tmp_path / "p"),
                 "--log", str(tmp_path / "log"), "--no-seed-users"])
    assert sqlite3.connect(db).execute("SELECT party FROM voters").fetchone()[0] == "MDP"
    assert "MDP_LIST_NOT_LOADED" in (tmp_path / "log").read_text()
