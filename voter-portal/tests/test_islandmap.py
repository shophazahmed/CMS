"""Island map: reading legacy-font Dhivehi house names off a PDF and showing each house on it."""
import io
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from app.islandmap import name_key, to_thaana  # noqa: E402
from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401
from tests.test_canvass import AGENT_PW, add_agent  # noqa: E402


def make_map():
    """A made-up map: Rose Villa's name once, Blue Ge's twice (ambiguous), plus a road name."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.draw_rect(pymupdf.Rect(40, 40, 360, 260))
    for text, x, y in (("egWminif", 100, 100), ("egulIn", 200, 150), ("egulIn", 300, 200), ("ugwm", 150, 250)):
        page.insert_text((x, y), text, fontsize=6)
    return doc.tobytes()


def test_legacy_font_decoding():
    assert to_thaana("egWmiSwb") == "ބަށިމާގެ"
    assert to_thaana("egWminif") == "ފިނިމާގެ"
    assert to_thaana("ބަށިމާގެ") == "ބަށިމާގެ"          # Unicode passes through
    assert name_key("ފިނިމާ ގެ.") == "ފިނިމާގެ"


def upload(client, blob):
    return client.post("/admin/map", data={"csrf_token": csrf(client, "/admin/map"), "map": (io.BytesIO(blob), "map.pdf")},
                       content_type="multipart/form-data", follow_redirects=True)


def test_upload_match_and_show(app):
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET house_name_dv = 'ފިނިމާގެ' WHERE house_name = 'Rose Villa'")
    conn.execute("UPDATE voters SET house_name_dv = 'ނީލުގެ' WHERE house_name = 'Blue Ge'")
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    assert "Upload map" in a.get("/admin/map").get_data(as_text=True)
    assert a.get("/map").status_code == 404                                  # no map yet
    r = upload(a, make_map())
    page = r.get_data(as_text=True)
    assert "4 names read, 1 houses found" in page
    assert "Not found on the map" in page and "Blue Ge" in page              # appears twice: not placed
    loc = connect(app.config["DATABASE"]).execute("SELECT * FROM house_locations").fetchall()
    assert [(r["house_name"], r["source"]) for r in loc] == [("Rose Villa", "auto")]

    house = a.get("/visits/house?h=Rose+Villa").get_data(as_text=True)
    assert "map-zoom" not in house and "map-btn" in house and "On the map" in house   # button only, no picture
    assert "Wrong spot? Set it" in a.get("/map?h=Rose+Villa").get_data(as_text=True)
    assert "Set its spot" in a.get("/map?h=Blue+Ge").get_data(as_text=True)
    assert "Found automatically" in a.get("/map?h=Rose+Villa").get_data(as_text=True)
    assert "Not on the map yet" in a.get("/visits/house?h=Blue+Ge").get_data(as_text=True)
    img = a.get("/map/house.png?h=Rose+Villa")
    assert img.status_code == 200 and img.mimetype == "image/png" and img.data[:4] == b"\x89PNG"
    assert a.get("/map/full.png").mimetype == "image/png"
    full = a.get("/map?h=Rose+Villa").get_data(as_text=True)
    assert "data-map-ring" in full and re.search(r'viewBox="[\d.]+ [\d.]+ [\d.]+ [\d.]+"', full)

    # an admin sets Blue Ge's spot by hand; it survives a new upload and re-matching
    token = csrf(a, "/")
    assert "data-map-set" in a.get("/map?h=Blue+Ge&set=1").get_data(as_text=True)
    a.post("/map/locate", data={"csrf_token": token, "h": "Blue Ge", "x": "300", "y": "198"})
    assert a.post("/map/locate", data={"csrf_token": token, "h": "Blue Ge", "x": "9999", "y": "1"}).status_code == 400
    upload(a, make_map())
    a.post("/admin/map/rematch", data={"csrf_token": token})
    rows = {r["house_name"]: (r["source"], r["x"]) for r in connect(app.config["DATABASE"]).execute(
        "SELECT * FROM house_locations")}
    assert rows["Blue Ge"] == ("manual", 300) and rows["Rose Villa"][0] == "auto"
    a.post("/map/unlocate", data={"csrf_token": token, "h": "Blue Ge"})
    assert "Blue Ge" not in {r[0] for r in connect(app.config["DATABASE"]).execute("SELECT house_name FROM house_locations")}
    assert "map_locate" in a.get("/admin/audit").get_data(as_text=True)

    # bad uploads
    assert "readable PDF" in upload(a, b"not a pdf").get_data(as_text=True)


def test_agents_see_their_houses_on_the_map_only(app):
    ali = add_agent(app, "ali")
    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET house_name_dv = 'ފިނިމާގެ' WHERE house_name = 'Rose Villa'")
    conn.execute("INSERT INTO house_assignments (house_name, user_id, assigned_at) VALUES ('Rose Villa', ?, 't')", (ali,))
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    upload(a, make_map())
    g = app.test_client()
    login(g, "ali", AGENT_PW)
    assert "map-btn" in g.get("/visits/house?h=Rose+Villa").get_data(as_text=True)
    assert "Wrong spot" not in g.get("/map?h=Rose+Villa").get_data(as_text=True)    # admins only
    assert "data-map-pending" not in g.get("/map?h=Rose+Villa").get_data(as_text=True)
    assert g.get("/map/house.png?h=Rose+Villa").status_code == 200
    assert g.get("/map/house.png?h=Blue+Ge").status_code == 404              # not their house
    assert g.get("/map?h=Rose+Villa").status_code == 200
    assert g.get("/map?h=Rose+Villa&set=1").get_data(as_text=True).count("data-map-set") == 0
    token = csrf(g, "/")
    assert g.post("/map/locate", data={"csrf_token": token, "h": "Rose Villa", "x": "1", "y": "1"}).status_code == 403
    assert g.get("/admin/map").status_code == 403
    u = app.test_client()
    login(u, "viewer", USER_PW)
    assert u.post("/admin/map", data={"csrf_token": csrf(u, "/")}).status_code == 403
    anon = app.test_client()
    assert anon.get("/map/full.png").status_code == 302                       # behind the login


def test_admin_can_place_houses_outside_the_campaign_view_and_map_buttons(app):
    from tests.test_campaign import set_parties

    conn = connect(app.config["DATABASE"])
    conn.execute("UPDATE voters SET house_name_dv = 'ފިނިމާގެ' WHERE house_name = 'Rose Villa'")
    conn.commit()
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    upload(a, make_map())
    set_parties(a, ["AAA"])                                   # Blue Ge's only voter is BBB: hidden
    page = a.get("/admin/map").get_data(as_text=True)
    assert "back=admin" in page and "Blue Ge" in page
    assert "data-map-set" in a.get("/map?h=Blue+Ge&set=1&back=admin").get_data(as_text=True)
    r = a.post("/map/locate", data={"csrf_token": csrf(a, "/"), "h": "Blue Ge", "x": "200", "y": "150", "back": "admin"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/map")
    assert a.get("/map?h=No+Such+House").status_code == 404

    # Map button on the house page and on located houses in the Visits list
    house = a.get("/visits/house?h=Rose+Villa").get_data(as_text=True)
    assert 'class="button primary map-btn"' in house
    listing = a.get("/visits").get_data(as_text=True)
    assert 'class="tag map-tag"' in listing and listing.count('class="tag map-tag"') == 1   # Rose Villa only
