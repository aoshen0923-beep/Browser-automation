import pymupdf

from quizpilot import pdftools
from quizpilot.ingest import html_to_text, ingest_path
from quizpilot.kb import KB


def make_pdf(path):
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page body {i + 1}")
        page.insert_text((72, 100), f"ends with Z{i + 1}")
    # Two raster images on page 3.
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.clear_with(200)
    png = pix.tobytes("png")
    doc[2].insert_image(pymupdf.Rect(100, 200, 200, 300), stream=png)
    doc[2].insert_image(pymupdf.Rect(100, 350, 200, 450), stream=png)
    # Printed page numbers start at 1 on PDF page 2 (like a book with a cover).
    doc.set_page_labels([{"startpage": 1, "prefix": "", "style": "D", "firstpagenum": 1}])
    doc.save(str(path))


def test_page_facts(tmp_path):
    path = tmp_path / "t.pdf"
    make_pdf(path)
    with pdftools.open_pdf(path) as doc:
        assert pdftools.summary(doc)["pages"] == 4
        info = pdftools.page_info(doc[2])
        assert info.images == 2
        assert info.last_char == "3"
        assert info.last_text == "ends with Z3"
        assert pdftools.find_label(doc, "2") == 3
        out = pdftools.render_page(doc, 1, tmp_path / "p1.png")
        assert out.exists()


def test_ingest_folder(tmp_path):
    make_pdf(tmp_path / "a.pdf")
    (tmp_path / "b.html").write_text("<html><title>T</title><script>x()</script><p>标准全文公开</p></html>", encoding="utf-8")
    (tmp_path / "c.bin").write_bytes(b"\0")
    with KB(":memory:") as kb:
        done = ingest_path(kb, tmp_path, module="11")
        assert {p.name for p in done} == {"a.pdf", "b.html"}
        hits = kb.search("Page body 3")
        assert hits[0].page == 3


def test_html_to_text():
    title, text = html_to_text("<title>Hi</title><style>a{}</style><div>one</div><div>two</div>")
    assert title == "Hi"
    assert text == "one\ntwo"


def test_a_login_page_is_never_read_as_a_pdf():
    import pytest

    from quizpilot import pdftools

    html = b"<!DOCTYPE html><html><body>Login to your account Email/Username Password " + b"x" * 5000 + b"</body></html>"
    with pytest.raises(pdftools.NotAPdf):
        pdftools.open_pdf(html)
    assert not pdftools.is_pdf(html)
    assert pdftools.is_pdf(b"\n%PDF-1.7\n...")
