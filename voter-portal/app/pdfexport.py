"""Simple table PDFs (A4 landscape, header row on every page, page numbers) with PyMuPDF, already a dependency."""
import html
import io

ROWS_FIRST, ROWS_PER_PAGE = 24, 27   # leaves room for a wrapped line or two; the title takes space on page 1
CSS = ("* {font-family: sans-serif;} h1 {font-size: 15px; margin: 0 0 2px 0;} p.sub {font-size: 8.5px; color: #555;"
       " margin: 0 0 8px 0;} table {border-collapse: collapse; width: 100%;} th {font-size: 8px; text-align: left;"
       " background-color: #e9eef0; border: 0.5px solid #999; padding: 3px;} td {font-size: 8px; border: 0.5px solid #bbb;"
       " padding: 2.5px 3px;}")


def _mupdf():
    try:
        import pymupdf
    except ImportError:  # older PyMuPDF
        import fitz as pymupdf
    return pymupdf


def table_pdf(title, subtitle, headers, rows, widths=None):
    """bytes of a PDF: a title and subtitle, then the table, ROWS_PER_PAGE rows a page with the header row repeated."""
    mu = _mupdf()
    esc = lambda v: html.escape("" if v is None else str(v))
    widths = widths or [100 / len(headers)] * len(headers)
    head = "<tr>" + "".join(f'<th style="width:{w}%">{esc(h)}</th>' for h, w in zip(headers, widths)) + "</tr>"
    chunks = [rows[:ROWS_FIRST]] + [rows[i:i + ROWS_PER_PAGE] for i in range(ROWS_FIRST, len(rows), ROWS_PER_PAGE)]
    page = mu.paper_rect("a4-l")
    where = page + (28, 30, -28, -34)
    buf = io.BytesIO()
    writer = mu.DocumentWriter(buf)
    for n, chunk in enumerate(chunks):
        top = f"<h1>{esc(title)}</h1><p class='sub'>{esc(subtitle)}</p>" if n == 0 else ""
        body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in chunk)
        if not chunk:
            body = f"<tr><td colspan='{len(headers)}'>Nobody in this list.</td></tr>"
        story = mu.Story(html=f"{top}<table>{head}{body}</table>", user_css=CSS)
        more = True
        while more:  # a chunk with very long cells can still run onto another page
            dev = writer.begin_page(page)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
    writer.close()
    doc = mu.open("pdf", buf.getvalue())
    for i, p in enumerate(doc, 1):
        p.insert_text((page.width - 110, page.height - 16), f"Page {i} of {len(doc)}", fontsize=7.5, color=(0.4, 0.4, 0.4))
    return doc.tobytes(deflate=True, garbage=3)
