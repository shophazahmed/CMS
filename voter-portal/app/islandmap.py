"""The island map: a PDF plot map whose house names are text in an old Dhivehi typewriter font.

An admin uploads the PDF once. Its labels are read with their positions and converted to Unicode
Thaana, then matched to each house's Dhivehi name (voters.house_name_dv). A house page then shows
the map zoomed in on the house with a red ring; the full map can be zoomed and, for admins, tapped
to set or fix a house's spot. The map stays behind the login.
"""
import hashlib
import io
import json
import os
import re
import unicodedata

from flask import Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import audit
from .auth import admin_required, login_required
from .db import get_db, now_iso

bp = Blueprint("islandmap", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS map_labels (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,          -- Unicode Thaana
    key  TEXT NOT NULL,          -- normalised for matching
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_map_labels_key ON map_labels (key);
CREATE TABLE IF NOT EXISTS house_locations (
    house_name TEXT PRIMARY KEY,
    x REAL NOT NULL, y REAL NOT NULL,   -- PDF points on page 1
    source TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
    set_by TEXT, set_at TEXT NOT NULL
);
"""

MAX_PDF_BYTES = 50 * 1024 * 1024
FULL_SCALE = 8          # full map image: PDF points x 8 (sharp at 400%)
ZOOM_SCALE = 8          # house close-up
ZOOM_HALF = (55, 36)    # half width / height of the close-up, in PDF points
RING = 6                # ring radius around the house, in PDF points

# Legacy Dhivehi typewriter fonts store each Thaana letter as a Latin key, and the text in reverse.
_LEGACY = {
    "h": "ހ", "S": "ށ", "n": "ނ", "r": "ރ", "b": "ބ", "L": "ޅ", "k": "ކ", "a": "އ", "v": "ވ", "m": "މ",
    "f": "ފ", "d": "ދ", "t": "ތ", "l": "ލ", "g": "ގ", "N": "ޏ", "s": "ސ", "D": "ޑ", "z": "ޒ", "T": "ޓ",
    "y": "ޔ", "p": "ޕ", "j": "ޖ", "C": "ޗ", "H": "ޙ", "K": "ޚ", "J": "ޛ", "R": "ޜ", "X": "ޝ", "B": "ޞ",
    "F": "ޟ", "Y": "ޠ", "Z": "ޡ", "A": "ޢ", "G": "ޣ", "Q": "ޤ", "V": "ޥ", "x": "ޘ",
    "w": "ަ", "W": "ާ", "i": "ި", "I": "ީ", "u": "ު", "U": "ޫ", "e": "ެ", "E": "ޭ", "o": "ޮ", "O": "ޯ", "c": "ް",
}


def to_thaana(text):
    """Legacy typewriter-font text -> Unicode Thaana (already-Unicode text passes through)."""
    if any("ހ" <= ch <= "޿" for ch in text):
        return text
    return "".join(_LEGACY.get(ch, ch) for ch in reversed(text))


def name_key(text):
    """For matching: Thaana letters only (no spaces, dots, ZWNJ or Latin)."""
    text = unicodedata.normalize("NFC", text or "")
    return re.sub(r"[^ހ-޿]", "", text)


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def map_dir():
    return current_app.config["MAP_DIR"]


def map_pdf():
    path = os.path.join(map_dir(), "map.pdf")
    return path if os.path.exists(path) else None


def has_map():
    return map_pdf() is not None


def _page():
    import pymupdf

    path = map_pdf()
    if not path:
        abort(404)
    if "map_doc" not in g:
        g.map_doc = pymupdf.open(path)
    return g.map_doc[0]


def _house_allowed_or_404(name):
    """Admins: any house in the voter list (also outside the campaign view, e.g. to set its spot).
    Everyone else: only houses they can open on the Visits page."""
    if not name:
        abort(404)
    if g.user["role"] == "admin":
        if not get_db().execute("SELECT 1 FROM voters WHERE house_name = ? LIMIT 1", (name,)).fetchone():
            abort(404)
        return
    from .visits import _visible_house_or_404

    _visible_house_or_404(name)


def located_houses():
    return {r[0] for r in get_db().execute("SELECT house_name FROM house_locations")}


def location(house):
    return get_db().execute("SELECT * FROM house_locations WHERE house_name = ?", (house,)).fetchone()


# --- reading the map and matching houses ---------------------------------------------

def read_labels(path):
    import pymupdf

    page = pymupdf.open(path)[0]
    out = []
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        text = to_thaana(word)
        key = name_key(text)
        if key:
            out.append((text, key, x0, y0, x1, y1))
    return out, (page.rect.width, page.rect.height)


def _house_keys():
    """{house_name: Dhivehi key} for every house with a Dhivehi name (the most common spelling)."""
    rows = get_db().execute(
        "SELECT house_name, house_name_dv, COUNT(*) AS n FROM voters WHERE house_name != '' AND house_name_dv != ''"
        " GROUP BY house_name, house_name_dv ORDER BY house_name, n DESC").fetchall()
    keys = {}
    for r in rows:
        keys.setdefault(r["house_name"], name_key(r["house_name_dv"]))
    return {h: k for h, k in keys.items() if k}


def match_houses():
    """Place every house whose Dhivehi name appears exactly once on the map (never over a manual spot).
    Returns counts."""
    db = get_db()
    by_key = {}
    for row in db.execute("SELECT key, x0, y0, x1, y1 FROM map_labels"):
        by_key.setdefault(row["key"], []).append(row)
    manual = {r[0] for r in db.execute("SELECT house_name FROM house_locations WHERE source = 'manual'")}
    db.execute("DELETE FROM house_locations WHERE source = 'auto'")
    stamp = now_iso()
    placed = ambiguous = 0
    for house, key in _house_keys().items():
        if house in manual:
            continue
        hits = by_key.get(key)
        if hits is None and key.endswith("ގެ"):          # 'ފިނިމާ' on the map for 'ފިނިމާގެ'
            hits = by_key.get(key[:-2])
        elif hits is None:
            hits = by_key.get(key + "ގެ")
        if not hits:
            continue
        if len(hits) > 1:
            ambiguous += 1
            continue
        h = hits[0]
        db.execute("INSERT INTO house_locations (house_name, x, y, source, set_by, set_at) VALUES (?, ?, ?, 'auto', NULL, ?)",
                   (house, (h["x0"] + h["x1"]) / 2, (h["y0"] + h["y1"]) / 2, stamp))
        placed += 1
    return {"placed": placed, "ambiguous": ambiguous, "manual": len(manual)}


def _clear_cache():
    cache = os.path.join(map_dir(), "cache")
    if os.path.isdir(cache):
        for name in os.listdir(cache):
            try:
                os.remove(os.path.join(cache, name))
            except OSError:
                pass


@bp.post("/admin/map")
@admin_required
def upload():
    import pymupdf

    upload = request.files.get("map")
    blob = upload.read(MAX_PDF_BYTES + 1) if upload else b""
    if not blob:
        flash("Choose the map PDF first.", "error")
        return redirect(url_for("islandmap.admin"))
    if len(blob) > MAX_PDF_BYTES:
        flash("The map must be 50 MB or smaller.", "error")
        return redirect(url_for("islandmap.admin"))
    try:
        doc = pymupdf.open(stream=blob, filetype="pdf")
        if doc.page_count < 1:
            raise ValueError
    except Exception:  # noqa: BLE001 - any unreadable file
        flash("That file isn't a readable PDF.", "error")
        return redirect(url_for("islandmap.admin"))
    os.makedirs(os.path.join(map_dir(), "cache"), exist_ok=True)
    tmp = os.path.join(map_dir(), "map.pdf.new")
    with open(tmp, "wb") as fh:
        fh.write(blob)
    labels, _ = read_labels(tmp)
    os.replace(tmp, os.path.join(map_dir(), "map.pdf"))
    _clear_cache()
    db = get_db()
    db.execute("DELETE FROM map_labels")
    db.executemany("INSERT INTO map_labels (text, key, x0, y0, x1, y1) VALUES (?, ?, ?, ?, ?, ?)", labels)
    counts = match_houses()
    audit.log("map_upload", detail={"labels": len(labels), **counts}, commit=False)
    db.commit()
    flash(f"Map saved: {len(labels)} names read, {counts['placed']} houses found on it automatically.", "success")
    return redirect(url_for("islandmap.admin"))


@bp.post("/admin/map/rematch")
@admin_required
def rematch():
    """Run the automatic matching again (e.g. after importing Dhivehi house names)."""
    if not has_map():
        abort(404)
    counts = match_houses()
    audit.log("map_rematch", detail=counts, commit=False)
    get_db().commit()
    flash(f"{counts['placed']} houses found on the map automatically.", "success")
    return redirect(url_for("islandmap.admin"))


@bp.route("/admin/map")
@admin_required
def admin():
    db = get_db()
    houses = [r[0] for r in db.execute(
        "SELECT DISTINCT house_name FROM voters WHERE house_name != '' ORDER BY house_name COLLATE NOCASE")]
    located = {r["house_name"]: r["source"] for r in db.execute("SELECT house_name, source FROM house_locations")}
    keys = _house_keys()
    missing = [(h, h in keys) for h in houses if h not in located]
    return render_template("map/admin.html", has_map=has_map(), total=len(houses), missing=missing,
                           auto=sum(1 for s in located.values() if s == "auto"),
                           manual=sum(1 for s in located.values() if s == "manual"),
                           labels=db.execute("SELECT COUNT(*) FROM map_labels").fetchone()[0])


# --- images ----------------------------------------------------------------------------

def _png(page, clip, scale):
    import pymupdf

    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False).tobytes("png")


def _cached(name, make):
    path = os.path.join(map_dir(), "cache", name)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = make()
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    with open(path, "rb") as fh:
        return fh.read()


def _image_response(data):
    resp = Response(data, mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


def map_box():
    """The part of the page with the island on it (drawings and labels), with a margin."""
    import pymupdf

    page = _page()

    def make():
        box = pymupdf.Rect()
        for d in page.get_drawings():
            box |= d["rect"]
        for w in page.get_text("words"):
            box |= pymupdf.Rect(w[:4])
        if box.is_empty:
            box = page.rect
        box = (box + (-12, -12, 12, 12)) & page.rect
        return json.dumps([box.x0, box.y0, box.x1, box.y1]).encode()

    if "map_box" not in g:
        g.map_box = pymupdf.Rect(json.loads(_cached("box.json", make)))
    return g.map_box


@bp.route("/map/full.png")
@login_required
def full_image():
    page, box = _page(), map_box()
    return _image_response(_cached("full.png", lambda: _png(page, box, FULL_SCALE)))


@bp.route("/map/house.png")
@login_required
def house_image():
    """Close-up of one house with a red ring (only for houses the viewer can open)."""
    from PIL import Image, ImageDraw

    name = request.args.get("h", "")
    _house_allowed_or_404(name)
    loc = location(name) or abort(404)
    page = _page()
    import pymupdf

    hw, hh = ZOOM_HALF
    clip = pymupdf.Rect(loc["x"] - hw, loc["y"] - hh, loc["x"] + hw, loc["y"] + hh) & page.rect

    def make():
        img = Image.open(io.BytesIO(_png(page, clip, ZOOM_SCALE))).convert("RGB")
        draw = ImageDraw.Draw(img)
        cx, cy = (loc["x"] - clip.x0) * ZOOM_SCALE, (loc["y"] - clip.y0) * ZOOM_SCALE
        r = RING * ZOOM_SCALE
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(214, 32, 32), width=max(4, ZOOM_SCALE))
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        return out.getvalue()

    key = "house-" + hashlib.sha1(f"{name}|{loc['x']:.2f}|{loc['y']:.2f}".encode()).hexdigest()[:20] + ".png"
    return _image_response(_cached(key, make))


# --- the full map page -------------------------------------------------------------------

@bp.route("/map")
@login_required
def full():
    if not has_map():
        abort(404)
    name = request.args.get("h", "")
    loc = None
    if name:
        _house_allowed_or_404(name)
        loc = location(name)
    box = map_box()
    setting = bool(name) and request.args.get("set") == "1" and g.user["role"] == "admin"
    return render_template("map/full.html", house=name, loc=loc, box=box, ring=RING, setting=setting,
                           width=round(box.width, 2), height=round(box.height, 2))


@bp.post("/map/locate")
@admin_required
def locate():
    """Save a house's spot: the point an admin tapped on the full map (PDF points)."""
    name = request.form.get("h", "")
    _house_allowed_or_404(name)
    try:
        x, y = float(request.form.get("x", "")), float(request.form.get("y", ""))
    except ValueError:
        abort(400)
    page = _page()
    if not (0 <= x <= page.rect.width and 0 <= y <= page.rect.height):
        abort(400)
    db = get_db()
    db.execute("INSERT INTO house_locations (house_name, x, y, source, set_by, set_at) VALUES (?, ?, ?, 'manual', ?, ?)"
               " ON CONFLICT(house_name) DO UPDATE SET x = excluded.x, y = excluded.y, source = 'manual',"
               " set_by = excluded.set_by, set_at = excluded.set_at",
               (name, x, y, g.user["username"], now_iso()))
    audit.log("map_locate", target=name, detail={"x": round(x, 1), "y": round(y, 1)}, commit=False)
    db.commit()
    flash(f"Map location saved for {name}.", "success")
    if request.form.get("back") == "admin":
        return redirect(url_for("islandmap.admin"))
    return redirect(url_for("visits.house", h=name))


@bp.post("/map/unlocate")
@admin_required
def unlocate():
    name = request.form.get("h", "")
    db = get_db()
    db.execute("DELETE FROM house_locations WHERE house_name = ?", (name,))
    audit.log("map_unlocate", target=name, commit=False)
    db.commit()
    flash(f"Map location removed for {name}.", "success")
    return redirect(url_for("visits.house", h=name))
