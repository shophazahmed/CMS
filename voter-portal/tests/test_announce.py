import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_app import ADMIN_PW, USER_PW, app, csrf, login  # noqa: E402,F401


def test_announcement_whatsapp_banner_and_seen(app):
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    token = csrf(a, "/announcements")
    r = a.post("/announcements", data={"csrf_token": token, "text": "Meeting tonight 9pm", "urgent": "1"})
    assert r.status_code == 302 and "sent=1" in r.headers["Location"]
    page = a.get("/announcements?sent=1").get_data(as_text=True)
    assert "https://wa.me/?text=" in page and "Seen by 0 of 1" in page
    assert "Meeting tonight 9pm" in unquote(page.split("https://wa.me/?text=")[1].split('"')[0])
    assert "urgent-banner" not in a.get("/").get_data(as_text=True)  # the writer doesn't get their own banner

    u = app.test_client()
    login(u, "viewer", USER_PW)
    home = u.get("/").get_data(as_text=True)
    assert "urgent-banner" in home and "Meeting tonight 9pm" in home
    assert "wa.me" not in u.get("/announcements").get_data(as_text=True)  # only admins send
    u.post("/announcements/1/seen", data={"csrf_token": csrf(u, "/"), "back": "/"})
    assert "urgent-banner" not in u.get("/").get_data(as_text=True)
    assert "Seen by 1 of 1" in a.get("/announcements").get_data(as_text=True)


def test_only_admins_post_and_take_down(app):
    u = app.test_client()
    login(u, "viewer", USER_PW)
    token = csrf(u, "/announcements")
    assert u.post("/announcements", data={"csrf_token": token, "text": "x"}).status_code == 403
    a = app.test_client()
    login(a, "boss", ADMIN_PW)
    t = csrf(a, "/announcements")
    a.post("/announcements", data={"csrf_token": t, "text": "Old news", "urgent": "1"})
    assert u.post("/announcements/1/end", data={"csrf_token": token}).status_code == 403
    a.post("/announcements/1/end", data={"csrf_token": t})
    assert "Old news" not in u.get("/").get_data(as_text=True)
    assert "Old news" not in u.get("/announcements").get_data(as_text=True)
