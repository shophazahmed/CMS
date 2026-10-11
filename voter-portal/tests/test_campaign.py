import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402

# Fixture voters: A200001 Aminath Alpha (AAA, Rose Villa), A200002 Moosa Beta (BBB, Blue Ge),
# A200003 (no party, Rose Villa)


def set_parties(client, parties):
    token = csrf(client, "/admin/settings")
    return client.post("/admin/settings", data={"csrf_token": token, "parties": parties, "mode": "selected"},
                       follow_redirects=True)


def cards(client, path="/"):
    return client.get(path).get_data(as_text=True).count('id="v-A2000')


def test_campaign_hides_other_parties(app):
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    page = a.get("/admin/settings").get_data(as_text=True)
    assert "No party recorded" in page and "AAA" in page and "BBB" in page
    r = set_parties(a, ["AAA"])
    assert "AAA voters only" in r.get_data(as_text=True) and "2 other voters hidden" in r.get_data(as_text=True)

    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert cards(u) == 1
    assert cards(u, "/?q=Moosa") == 0
    assert u.get("/voters/A200002").status_code == 404              # direct link to a hidden voter
    token = csrf(u, "/")
    assert u.post("/voters/A200002/canvass", data={"csrf_token": token, "status": "contacted"}).status_code == 404
    assert "Show all parties" not in u.get("/").get_data(as_text=True)   # users can't switch it off
    assert u.post("/admin/view-all", data={"csrf_token": token}).status_code == 403
    dash = u.get("/dashboard").get_data(as_text=True)
    assert "Blue Ge" not in dash and "Rose Villa" in dash and "0 of 1" in dash

    # admin: campaign view by default, can switch to all parties for their own session only
    assert cards(a) == 1 and "Campaign view: <strong>AAA</strong>" in a.get("/").get_data(as_text=True)
    token = csrf(a, "/")
    a.post("/admin/view-all", data={"csrf_token": token, "back": "/"})
    assert cards(a) == 3 and a.get("/voters/A200002").status_code == 200
    assert "Moosa" in a.get("/export.csv").get_data(as_text=True)
    assert cards(u) == 1                                             # other users unaffected
    a.post("/admin/view-all", data={"csrf_token": token})
    assert cards(a) == 1 and "Moosa" not in a.get("/export.csv").get_data(as_text=True)

    # no party can be part of the campaign too; clearing shows everyone again
    set_parties(a, ["AAA", "(none)"])
    assert cards(u) == 2
    token = csrf(a, "/admin/settings")
    a.post("/admin/settings", data={"csrf_token": token, "mode": "all"})
    assert cards(u) == 3
    assert "campaign_parties" in a.get("/admin/audit").get_data(as_text=True)


def test_campaign_and_agent_scopes_combine(app):
    agent_id = add_agent(app)
    conn = connect(app.config["DATABASE"])
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES ('Rose Villa', ?, 't')",
                 (agent_id,))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    set_parties(a, ["AAA"])
    g = app.test_client()
    login(g, "ali", AGENT_PW)
    assert cards(g) == 1                       # Rose Villa has 2 voters, only 1 is AAA
    assert g.get("/voters/A200003").status_code == 404
    assert "0/1" in g.get("/my").get_data(as_text=True)
    assert "1 voters" in a.get("/admin/agents").get_data(as_text=True)
