"""EPUB renderer (render/epub.py, ARCHITECTURE §6.2): structure and firmware rules.

epubcheck and the real CrossPoint parsers run in tests/test_render_validation.py.
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from lxml import etree

from sendtox3.render.epub import DIGEST_CREATOR, MAX_TITLE_BYTES, digest_title, render_epub
from sendtox3.textmodel import Document, Heading, ListItem, Paragraph, Preformatted, Quote, Rule

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "x": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "epub": "http://www.idpf.org/2007/ops",
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
}
ALLOWED_BODY_TAGS = {"h1", "h2", "h3", "p", "ul", "ol", "li", "blockquote", "hr", "br", "i"}
BLOCK_CONTAINERS = {"body", "ul", "ol", "blockquote"}  # must never hold bare text
NBSP = "\u00a0"


def _doc(**kw) -> Document:
    base = dict(
        title="How E-Ink Works",
        byline="Jane Doe",
        site="Example News",
        source_url="https://www.example.com/articles/e-ink?id=7&x=1",
        published="2026-10-24",
        lang="en",
        blocks=[Paragraph("Electrophoretic displays move charged particles."), Heading("Sub", 2), Paragraph("More.")],
    )
    base.update(kw)
    return Document(**base)


class Book:
    def __init__(self, data: bytes):
        self.data = data
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        self.names = self.zip.namelist()

    def text(self, name: str) -> str:
        return self.zip.read(name).decode("utf-8")

    def xml(self, name: str):
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        return etree.fromstring(self.zip.read(name), parser)

    @property
    def opf(self):
        return self.xml("OEBPS/content.opf")

    def meta(self, tag: str) -> list[str]:
        return [e.text for e in self.opf.findall(f"opf:metadata/dc:{tag}", NS)]

    def chapters(self) -> list[str]:
        opf = self.opf
        items = {i.get("id"): i.get("href") for i in opf.findall("opf:manifest/opf:item", NS)}
        return ["OEBPS/" + items[r.get("idref")] for r in opf.findall("opf:spine/opf:itemref", NS)]


def _book(docs, **kw) -> Book:
    return Book(render_epub(docs, **kw))


# --------------------------------------------------------------------------------------
# Container
# --------------------------------------------------------------------------------------


def test_zip_layout():
    b = _book([_doc()])
    assert b.names == [
        "mimetype",
        "META-INF/container.xml",
        "OEBPS/content.opf",
        "OEBPS/nav.xhtml",
        "OEBPS/toc.ncx",
        "OEBPS/style.css",
        "OEBPS/text/item-001.xhtml",
    ]
    infos = b.zip.infolist()
    assert infos[0].compress_type == zipfile.ZIP_STORED
    assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in infos[1:])
    assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in infos)
    # raw local header of the first entry: "mimetype" stored, no extra field
    sig, _, flags, method, _, _, _, _, _, nlen, xlen = struct.unpack("<IHHHHHIIIHH", b.data[:30])
    assert sig == 0x04034B50 and method == 0 and nlen == 8 and xlen == 0 and flags & 0x08 == 0
    assert b.data[30:58] == b"mimetypeapplication/epub+zip"
    assert b"PK\x06\x06" not in b.data  # no ZIP64 end record
    assert b.zip.testzip() is None


def test_container_xml():
    root = _book([_doc()]).xml("META-INF/container.xml")
    rf = root.find("c:rootfiles/c:rootfile", NS)
    assert rf.get("full-path") == "OEBPS/content.opf"
    assert rf.get("media-type") == "application/oebps-package+xml"
    assert root.tag == "{urn:oasis:names:tc:opendocument:xmlns:container}container"  # unprefixed


def test_every_xml_part_parses_and_has_no_named_entities():
    b = _book([_doc(title="Tom & Jerry <3 \"quotes\" 'apos'", blocks=[Paragraph("a & b < c > d \" e ' f")])])
    for name in b.names:
        if name.endswith((".xml", ".opf", ".ncx", ".xhtml")):
            raw = b.text(name)
            b.xml(name)
            for m in re.finditer(r"&([^;\s]*);", raw):
                assert m.group(1) in {"amp", "lt", "gt", "quot"} or m.group(1).startswith("#"), (name, m.group(0))
            assert "./" not in re.sub(r"\.\./style\.css", "", raw)  # no "./" hrefs


# --------------------------------------------------------------------------------------
# OPF metadata
# --------------------------------------------------------------------------------------


def test_single_item_metadata():
    b = _book([_doc()])
    assert b.meta("title") == ["How E-Ink Works"]
    assert b.meta("creator") == ["Jane Doe"]
    assert b.meta("language") == ["en"]
    assert b.meta("date") == ["2026-10-24"]
    assert b.meta("publisher") == ["Example News"]
    assert b.meta("source") == ["https://www.example.com/articles/e-ink?id=7&x=1"]
    ident = b.meta("identifier")[0]
    assert re.fullmatch(r"urn:uuid:[0-9a-f-]{36}", ident)
    opf = b.opf
    assert opf.get("version") == "3.0" and opf.get("unique-identifier") == "bookid"
    modified = opf.find("opf:metadata/opf:meta[@property='dcterms:modified']", NS).text
    assert modified == "2026-10-24T00:00:00Z"


def test_creator_falls_back_to_site_then_host():
    assert _book([_doc(byline="")]).meta("creator") == ["Example News"]
    assert _book([_doc(byline="", site="")]).meta("creator") == ["example.com"]
    assert _book([_doc(byline="", site="", source_url="")]).meta("creator") == []


def test_language_from_doc():
    assert _book([_doc(lang="de")]).meta("language") == ["de"]
    assert _book([_doc(lang="pt_br")]).meta("language") == ["pt-BR"]
    assert _book([_doc(lang="zh-hant-tw")]).meta("language") == ["zh-Hant-TW"]
    assert _book([_doc(lang="")]).meta("language") == ["en"]
    assert _book([_doc(lang="not a lang!")]).meta("language") == ["en"]
    chapter = _book([_doc(lang="ru")]).xml("OEBPS/text/item-001.xhtml")
    assert chapter.get("lang") == "ru" and chapter.get("{http://www.w3.org/XML/1998/namespace}lang") == "ru"


def test_invalid_dates_dropped_or_truncated():
    assert _book([_doc(published="2026-02-31")]).meta("date") == ["2026-02"]
    assert _book([_doc(published="soon")]).meta("date") == []
    assert _book([_doc(published="0000-01-01")]).meta("date") == []


def test_title_capped_at_500_bytes():
    b = _book([_doc(title="\u00e9" * 400)])
    title = b.meta("title")[0]
    assert len(title.encode("utf-8")) <= MAX_TITLE_BYTES
    assert title.endswith("\u2026")


def test_manifest_and_spine():
    b = _book([_doc()])
    opf = b.opf
    items = {i.get("id"): i for i in opf.findall("opf:manifest/opf:item", NS)}
    assert items["nav"].get("properties") == "nav" and items["nav"].get("href") == "nav.xhtml"
    assert items["ncx"].get("media-type") == "application/x-dtbncx+xml"
    assert items["css"].get("href") == "style.css"
    spine = opf.find("opf:spine", NS)
    assert spine.get("toc") == "ncx"
    refs = [r.get("idref") for r in spine]
    assert refs == ["item-001"]  # nav not in the spine
    for i in items.values():
        assert not i.get("href").startswith(("./", "/"))
    assert b.text("OEBPS/style.css") == "blockquote{margin-left:1.5em;margin-right:1.5em}\n"


def test_identifier_handling():
    u = "12345678-1234-5678-1234-567812345678"
    assert _book([_doc()], identifier=u).meta("identifier") == [f"urn:uuid:{u}"]
    assert _book([_doc()], identifier=f"urn:uuid:{u.upper()}").meta("identifier") == [f"urn:uuid:{u}"]
    assert _book([_doc()], identifier="abcdefghij").meta("identifier") == ["urn:sendtox3:abcdefghij"]
    assert _book([_doc()], identifier="isbn:123").meta("identifier") == ["isbn:123"]
    a = _book([_doc()]).meta("identifier")
    assert a == _book([_doc(title="other title")]).meta("identifier")  # keyed by URL
    assert a != _book([_doc(source_url="https://example.com/other")]).meta("identifier")
    n1 = _book([_doc(source_url="", blocks=[Paragraph("one")])]).meta("identifier")
    n2 = _book([_doc(source_url="", blocks=[Paragraph("two")])]).meta("identifier")
    assert n1 != n2  # same-titled notes with different text
    ncx = _book([_doc()], identifier=u).xml("OEBPS/toc.ncx")
    assert ncx.find("ncx:head/ncx:meta[@name='dtb:uid']", NS).get("content") == f"urn:uuid:{u}"


def test_modified_parameter():
    when = datetime(2026, 10, 25, 14, 30, 5, 123, tzinfo=timezone(timedelta(hours=2)))
    b = _book([_doc()], modified=when)
    assert b.opf.find("opf:metadata/opf:meta[@property='dcterms:modified']", NS).text == "2026-10-25T12:30:05Z"
    b = _book([_doc()], modified=0)
    assert b.opf.find("opf:metadata/opf:meta[@property='dcterms:modified']", NS).text == "1970-01-01T00:00:00Z"
    b = _book([_doc(published="")])
    assert b.opf.find("opf:metadata/opf:meta[@property='dcterms:modified']", NS).text == "1980-01-01T00:00:00Z"
    with pytest.raises(TypeError):
        render_epub([_doc()], modified="yesterday")  # type: ignore[arg-type]


def test_deterministic_bytes():
    docs = [_doc(), _doc(title="Second", source_url="https://example.org/2")]
    assert render_epub([docs[0]]) == render_epub([docs[0]])
    assert render_epub(docs, modified=1_761_400_000) == render_epub(docs, modified=1_761_400_000)
    assert render_epub(docs) == render_epub(list(docs))


def test_empty_input_rejected():
    with pytest.raises(ValueError):
        render_epub([])


# --------------------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------------------


def test_nav_and_ncx_single():
    b = _book([_doc(title="Line\nbreak title")])
    nav = b.xml("OEBPS/nav.xhtml")
    navs = nav.findall(".//x:nav", NS)
    assert len(navs) == 1 and navs[0].get("{http://www.idpf.org/2007/ops}type") == "toc"
    links = navs[0].findall("x:ol/x:li/x:a", NS)
    assert [a.text for a in links] == ["Line break title"]
    assert [a.get("href") for a in links] == ["text/item-001.xhtml"]
    ncx = b.xml("OEBPS/toc.ncx")
    pts = ncx.findall("ncx:navMap/ncx:navPoint", NS)
    assert [p.find("ncx:navLabel/ncx:text", NS).text for p in pts] == ["Line break title"]
    assert [p.find("ncx:content", NS).get("src") for p in pts] == ["text/item-001.xhtml"]
    assert ncx.find("ncx:docTitle/ncx:text", NS).text == "Line break title"


def test_digest():
    docs = [
        _doc(title="First", source_url="https://a.example/1", lang="en"),
        _doc(title="Second & more", source_url="https://b.example/2", lang="de"),
        _doc(title="Third", source_url="", lang="de", byline="", site=""),
    ]
    when = datetime(2026, 10, 25, 9, 5, tzinfo=timezone.utc)
    b = _book(docs, modified=when)
    local = when.astimezone()
    assert b.meta("title") == [f"Inbox: 3 items ({local:%Y-%m-%d %H:%M})"]
    assert b.meta("creator") == [DIGEST_CREATOR]
    assert b.meta("language") == ["de"]  # most common
    assert b.meta("source") == ["https://a.example/1", "https://b.example/2"]
    assert b.meta("date") == [f"{local:%Y-%m-%d}"]
    chapters = b.chapters()
    assert chapters == ["OEBPS/text/item-001.xhtml", "OEBPS/text/item-002.xhtml", "OEBPS/text/item-003.xhtml"]
    labels = [a.text for a in b.xml("OEBPS/nav.xhtml").findall(".//x:nav/x:ol/x:li/x:a", NS)]
    assert labels == ["First", "Second & more", "Third"]
    ncx = b.xml("OEBPS/toc.ncx")
    pts = ncx.findall("ncx:navMap/ncx:navPoint", NS)
    assert [p.get("playOrder") for p in pts] == ["1", "2", "3"]
    assert [p.find("ncx:navLabel/ncx:text", NS).text for p in pts] == labels
    for href, doc in zip(chapters, docs):
        ch = b.xml(href)
        assert ch.find("x:body/x:h1", NS).text == doc.title
        assert ch.get("lang") == doc.lang
    # no splitting of anything else: one spine item per document
    assert len(b.opf.findall("opf:spine/opf:itemref", NS)) == 3


def test_digest_title_without_time_and_override():
    docs = [_doc(), _doc(title="B")]
    assert _book(docs).meta("title") == ["Inbox: 2 items"]
    assert _book(docs, book_title="My digest").meta("title") == ["My digest"]
    assert _book([_doc()], book_title="Override").meta("title") == ["Override"]
    assert digest_title(1) == "Inbox: 1 item"


def test_digest_with_many_items_numbering():
    docs = [_doc(title=f"T{i}", source_url=f"https://e.example/{i}") for i in range(1200)]
    b = _book(docs)
    ch = b.chapters()
    assert ch[0] == "OEBPS/text/item-0001.xhtml" and ch[-1] == "OEBPS/text/item-1200.xhtml"
    assert len(set(ch)) == 1200


# --------------------------------------------------------------------------------------
# Chapter body
# --------------------------------------------------------------------------------------


def _body(doc: Document):
    b = _book([doc])
    return b.xml("OEBPS/text/item-001.xhtml").find("x:body", NS), b.text("OEBPS/text/item-001.xhtml")


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _check_body_rules(body) -> None:
    for el in body.iter():
        if el is body:
            continue
        tag = _local(el.tag)
        assert tag in ALLOWED_BODY_TAGS, tag
        assert el.tag.startswith("{http://www.w3.org/1999/xhtml}")
        if tag in BLOCK_CONTAINERS:
            assert not (el.text or "").strip(), f"bare text in <{tag}>: {el.text!r}"
        if _local(el.getparent().tag) in BLOCK_CONTAINERS:
            assert not (el.tail or "").strip(), f"bare text after <{tag}>: {el.tail!r}"
        if tag == "li":
            # text directly before a nested list must be separated by whitespace
            for child in el:
                if _local(child.tag) in ("ul", "ol"):
                    prev = child.getprevious()
                    before = (prev.tail if prev is not None else el.text) or ""
                    assert before == "" or before[-1].isspace()
    assert not (body.text or "").strip()


def test_chapter_header_and_footer():
    body, raw = _body(_doc(notes=["Paywalled: only the preview was available", "Second note"]))
    kids = list(body)
    tags = [_local(k.tag) for k in kids]
    assert tags[:6] == ["h1", "p", "p", "p", "p", "hr"]
    assert kids[0].text == "How E-Ink Works"
    assert kids[1][0].tag.endswith("i") and kids[1][0].text == "Jane Doe"
    assert kids[2][0].text == "Example News \u00b7 2026-10-24"
    assert kids[3][0].text == "Note: Paywalled: only the preview was available"
    assert kids[4][0].text == "Note: Second note"
    assert tags[-2:] == ["hr", "p"] and kids[-1][0].text == "Source: example.com"
    assert "https://" not in raw.split("<body>")[1]  # never a long URL in the body
    _check_body_rules(body)


def test_chapter_header_minimal():
    body, raw = _body(_doc(byline="", site="", published="", source_url="", blocks=[Paragraph("x")]))
    assert [_local(k.tag) for k in body] == ["h1", "hr", "p"]
    assert "Source:" not in raw


def test_block_mapping():
    doc = _doc(
        blocks=[
            Heading("Top", 1),
            Heading("Mid", 2),
            Heading("Low", 3),
            Paragraph("para"),
            Quote("q1"),
            Quote("q2"),
            Rule(),
            Paragraph(""),
            Heading("", 2),
            Paragraph("after"),  # a final Rule would merge with the Source line's <hr/>
        ]
    )
    body, _ = _body(doc)
    seq = [(_local(k.tag), (k.text or "")) for k in body][4:-2]
    assert seq == [("h2", "Top"), ("h2", "Mid"), ("h3", "Low"), ("p", "para"), ("blockquote", "\n"), ("hr", ""),
                   ("p", "after")]
    bq = body.find("x:blockquote", NS)
    assert [p.text for p in bq.findall("x:p", NS)] == ["q1", "q2"]
    assert len(body.findall("x:h1", NS)) == 1
    _check_body_rules(body)


def test_preformatted_lines_with_nbsp():
    body, _ = _body(_doc(blocks=[Preformatted("def f():\n    return  1\n\n\tx")]))
    ps = [p.text for p in body.findall("x:p", NS)][2:-1]
    assert ps == ["def f():", NBSP * 4 + "return" + NBSP + " 1", NBSP, NBSP * 4 + "x"]
    assert body.find(".//x:pre", NS) is None
    _check_body_rules(body)


def test_nested_lists_inside_parent_li():
    blocks = [
        ListItem("a"),
        ListItem("a.1", depth=1),
        ListItem("a.1.i", depth=2, ordered=True, index=1),
        ListItem("a.2", depth=1),
        ListItem("b"),
        ListItem("jump", depth=3),  # malformed jump nests one level only
        ListItem("c"),
    ]
    body, raw = _body(_doc(blocks=blocks))
    _check_body_rules(body)
    top = body.findall("x:ul", NS)
    assert len(top) == 1
    lis = top[0].findall("x:li", NS)
    assert [li.text.strip() for li in lis] == ["a", "b", "c"]
    sub = lis[0].find("x:ul", NS)
    assert [li.text.strip() for li in sub.findall("x:li", NS)] == ["a.1", "a.2"]
    deep = sub.find("x:li", NS).find("x:ol", NS)
    assert [li.text.strip() for li in deep.findall("x:li", NS)] == ["a.1.i"]
    assert [li.text.strip() for li in lis[1].find("x:ul", NS).findall("x:li", NS)] == ["jump"]


def test_ordered_list_start_attribute():
    def ol_starts(blocks):
        body, _ = _body(_doc(blocks=blocks))
        _check_body_rules(body)
        return [ol.get("start") for ol in body.iter("{http://www.w3.org/1999/xhtml}ol")]

    assert ol_starts([ListItem("x", ordered=True, index=1), ListItem("y", ordered=True, index=2)]) == [None]
    assert ol_starts([ListItem("x", ordered=True, index=4), ListItem("y", ordered=True, index=5)]) == ["4"]
    assert ol_starts([ListItem("x", ordered=True, index=0)]) == ["0"]
    assert ol_starts([ListItem("x", ordered=True, index=-3)]) == [None]
    assert ol_starts([ListItem("x", ordered=True, index=True)]) == [None]
    # ul then ol at the same depth -> two lists
    body, _ = _body(_doc(blocks=[ListItem("u"), ListItem("o", ordered=True, index=7)]))
    assert [_local(k.tag) for k in body][4:6] == ["ul", "ol"]


def test_list_then_quote_then_list():
    body, _ = _body(_doc(blocks=[ListItem("a"), Quote("q"), ListItem("b"), Paragraph("p")]))
    _check_body_rules(body)
    assert [_local(k.tag) for k in body][4:8] == ["ul", "blockquote", "ul", "p"]


def test_text_cleaning_and_escaping_in_body():
    body, raw = _body(
        _doc(
            title="A <b>bold</b> & 'title'",
            blocks=[Paragraph("x \U0001f600 y\u200b z \x00 & <i>not markup</i>"), ListItem("l & <m>")],
        )
    )
    assert body.find("x:h1", NS).text == "A <b>bold</b> & 'title'"
    assert body.findall("x:p", NS)[2].text == "x y z & <i>not markup</i>"
    assert "&lt;i&gt;not markup&lt;/i&gt;" in raw
    assert "<b>" not in raw
    _check_body_rules(body)


def test_lone_surrogates_and_nonchars_removed():
    b = _book([_doc(title="a\ud800b", blocks=[Paragraph("c\uffffd\ufdd0e")])])
    assert b.meta("title") == ["ab"]
    body = b.xml("OEBPS/text/item-001.xhtml").find("x:body", NS)
    assert body.findall("x:p", NS)[2].text == "cde"


def test_lowercase_tags_only_everywhere():
    b = _book([_doc(blocks=[ListItem("a"), ListItem("b", depth=1), Quote("q"), Preformatted(" x"), Rule()])])
    for name in b.names:
        if name.endswith(".xhtml"):
            raw = b.text(name)
            for tag in re.findall(r"</?([A-Za-z][\w:.-]*)", raw):
                assert tag == tag.lower(), (name, tag)
                assert ":" not in tag, (name, tag)  # unprefixed element names only


def test_large_document_single_spine_item():
    text = " ".join(f"Sentence number {i} of a very long article." for i in range(20000))
    b = _book([_doc(blocks=[Paragraph(text)])])
    assert b.chapters() == ["OEBPS/text/item-001.xhtml"]


def test_long_document_is_split_into_chapters_at_headings():
    import io as _io
    import zipfile as _zipfile

    from sendtox3.render.epub import SPLIT_THRESHOLD_BYTES, render_epub as _render
    from sendtox3.textmodel import Document as _Doc, Heading as _H, Paragraph as _P

    blocks = []
    for c in range(1, 5):
        blocks.append(_H(f"Part {c}", 2))
        blocks += [_P("word " * 400) for _ in range(10)]
    doc = _Doc(title="Long", blocks=blocks, source_url="https://example.com/x")
    assert sum(len(b.text) for b in blocks) > SPLIT_THRESHOLD_BYTES
    data = _render([doc])
    z = _zipfile.ZipFile(_io.BytesIO(data))
    chapters = sorted(n for n in z.namelist() if n.startswith("OEBPS/text/"))
    assert len(chapters) == 4
    nav = z.read("OEBPS/nav.xhtml").decode()
    assert ">Long<" in nav and ">Part 2<" in nav and ">Part 4<" in nav
    first = z.read("OEBPS/text/item-001.xhtml").decode()
    last = z.read("OEBPS/text/item-001-004.xhtml").decode()
    assert "<h1>Long</h1>" in first and "<h1>" not in last  # title page only once
    assert "Source: example.com" in last and "Source:" not in first  # footer only at the end
    assert _render([doc]) == data  # still deterministic


def test_short_document_stays_one_chapter():
    import io as _io
    import zipfile as _zipfile

    from sendtox3.render.epub import render_epub as _render
    from sendtox3.textmodel import Document as _Doc, Heading as _H, Paragraph as _P

    doc = _Doc(title="Short", blocks=[_H("A", 2), _P("x " * 100), _H("B", 2), _P("y " * 100)])
    z = _zipfile.ZipFile(_io.BytesIO(_render([doc])))
    assert len([n for n in z.namelist() if n.startswith("OEBPS/text/")]) == 1
