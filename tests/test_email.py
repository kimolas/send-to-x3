"""extract/email.py: RFC 822 -> IngestRequests (ARCHITECTURE §9, §14).

The .eml corpus in tests/fixtures/emails is synthetic (see its README); messages built
in code below use the stdlib so their MIME structure is exactly what we intend.
"""

from __future__ import annotations

import io
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from sendtox3.extract import email as em
from sendtox3.extract.email import (
    clean_subject,
    decode_text_bytes,
    parse_email,
    unflow,
)

from pathlib import Path

EMAILS = Path(__file__).parent / "fixtures" / "emails"


def load(name: str) -> bytes:
    return (EMAILS / name).read_bytes()


def tiny_pdf(text: str = "Hello from a PDF") -> bytes:
    """A minimal, valid one-page PDF (correct xref offsets) with one line of text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Title (Quarterly notes) /Producer (sendtox3 tests) >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def msg(subject: str = "Note", frm: str = "Qiong Example <qiong@example.org>", **headers: str) -> EmailMessage:
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "x3reader+k7q2@gmail.com"
    m["Subject"] = subject
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    return m


def raw(m: EmailMessage) -> bytes:
    return m.as_bytes(policy=SMTP)


# ------------------------------------------------------------------------- fixtures


def test_tiny_pdf_is_valid():
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(tiny_pdf("abc")))
    assert len(reader.pages) == 1
    assert "abc" in reader.pages[0].extract_text()
    assert reader.metadata.title == "Quarterly notes"


def test_gmail_url_only_quoted_printable_split_url():
    p = parse_email(load("gmail_url_only.eml"))
    assert p.subject == "Reading on e-ink — a long read"
    assert p.from_addr == "qiong@example.org" and p.from_name == "Qiong Example"
    assert p.message_id == "<CAF0001@mail.gmail.com>"
    assert len(p.requests) == 1
    r = p.requests[0]
    assert r.kind == "url" and r.source == "email"
    # QP soft line breaks inside the URL are joined; "=3D" decoded; "Sent from my iPhone" ignored.
    assert r.url == (
        "https://news.example.com/2026/10/25/a-very-long-article-slug-about-reading-on-e-ink-devices"
        "?utm_source=share&utm_medium=ios_app&ref=readlater"
    )
    assert r.title == "Reading on e-ink — a long read"
    assert r.sender == "Qiong Example <qiong@example.org>"


def test_gmail_inline_forward_strips_banner_preamble_and_both_signatures():
    p = parse_email(load("gmail_forward_inline.eml"))
    assert p.subject == "Notes from the café"
    [r] = p.requests
    assert r.kind == "text"
    assert r.title == "Notes from the café"
    assert r.sender == "Alice Writer <alice@example.net>"  # the original author, not the forwarder
    assert r.text.startswith("Here are my notes from Thursday’s session at the café.")
    assert "Short paragraphs are kinder." in r.text
    for junk in ("Forwarded message", "From:", "Date:", "Thought you'd like this", "Senior Researcher",
                 "Product Notes Weekly", "\n--"):  # fmt: skip
        assert junk not in r.text


def test_gmail_inline_forward_html_path():
    """Same forward, HTML only (as Gmail sends when the plain part is missing)."""
    p = parse_email(_html_only("gmail_forward_inline.eml"))
    [r] = p.requests
    assert r.kind == "html"
    assert r.sender == "Alice Writer <alice@example.net>"
    text = em._html_visible_text(r.html)
    assert text.startswith("Here are my notes from Thursday’s session")
    for junk in (
        "Forwarded message",
        "From:",
        "Subject:",
        "Thought you'd like this",
        "Product Notes Weekly",
        "Senior Researcher",
    ):
        assert junk not in text
    assert "<title>Notes from the café</title>" in r.html


def test_apple_flowed_base64_forward():
    p = parse_email(load("apple_forward_flowed.eml"))
    [r] = p.requests
    assert r.kind == "text" and r.title == "Long thoughts" and r.sender == "Bob Author <bob@example.com>"
    assert r.text == (
        "This is a long paragraph that the mail client wrapped at seventy-two characters using format=flowed "
        "soft breaks, which a naive reader would show as ragged short lines.\n\n"
        "> quoted line from an earlier message that also flows\n\n"
        "From the space-stuffed line onwards, the second paragraph continues here."
    )
    assert "\r" not in r.text


def test_apple_html_forward_unwraps_cite_blockquote_and_drops_header_divs():
    p = parse_email(_html_only("apple_forward_html.eml"))
    [r] = p.requests
    assert r.kind == "html"
    assert r.title == "On slow reading" and r.sender == "Dana Essayist <dana@example.com>"
    text = em._html_visible_text(r.html)
    assert text.startswith("Why slow reading matters")
    assert "Begin forwarded message" not in text and "From:" not in text and "Date:" not in text
    assert "<blockquote" not in r.html  # forwarded content is not a quotation
    assert "<h2>Why slow reading matters</h2>" in r.html


def test_apple_html_forward_prefers_full_plain_alternative():
    [r] = parse_email(load("apple_forward_html.eml")).requests
    assert r.kind == "text"
    assert r.text.startswith("Why slow reading matters\n\nSlow reading is a practice of attention.")
    assert "Begin forwarded message" not in r.text


def test_outlook_forward_plain_cp1252_qp():
    p = parse_email(load("outlook_forward.eml"))
    [r] = p.requests
    assert r.kind == "text"
    assert r.title == "Plan for the reading group" and r.sender == "Carol Planner <carol@example.com>"
    assert r.text == (
        "Hello all,\n\nThe plan for next week — we meet at the café at 10. Bring the essay you liked most "
        "this month and one question about it.\n\nThanks,\nCarol"
    )


def test_outlook_forward_html_path():
    [r] = parse_email(_html_only("outlook_forward.eml")).requests
    assert r.kind == "html" and r.sender == "Carol Planner <carol@example.com>"
    text = em._html_visible_text(r.html)
    assert text.startswith("Hello all,")
    for junk in ("FYI", "From:", "Sent:", "Subject:", "Outlook for iOS"):
        assert junk not in text
    assert "<hr" not in r.html
    assert "café" in text and "—" in text  # windows-1252 decoded


def test_forward_as_attachment_uses_inner_message():
    p = parse_email(load("forward_as_attachment.eml"))
    [r] = p.requests  # outer "see attached" note is dropped
    assert r.kind == "html"
    assert r.title == "Weekly digest #42" and r.sender == "Example Newsletter <news@example.net>"
    text = em._html_visible_text(r.html)
    assert "This week we look at three essays" in text
    assert "View in browser" not in text and "Unsubscribe" not in text and "You received this" not in text
    assert "<img" not in r.html


def test_flowed_delsp_yes():
    [r] = parse_email(load("flowed_delsp_yes.eml")).requests
    assert r.text == (
        "Supercalifragilistic words get split by DelSp=Yes clients: extraordinarily long words survive. "
        "A second sentence flows onto the next line, and a line that starts with a space is space-stuffed."
        "\n\nNew paragraph."
    )


def test_url_plus_few_words_is_a_link_and_subject_tag_is_removed():
    # One link plus a short comment: handed to the Processor like a text-only HTTP share
    # ("auto": it fetches the article and keeps the comment).
    p = parse_email(load("qp_split_url_words.eml"))
    assert p.raw_subject == "[x3] Why e-ink is good"
    [r] = p.requests
    assert r.kind == "auto" and r.url == ""
    assert r.text == (
        "Check this out, it's great for the weekend:\n\n"
        "https://blog.example.com/posts/2026/why-e-ink-is-good-for-long-form-reading-and-other-notes.html"
    )
    assert r.title == "Why e-ink is good"


def test_base64_note_and_re_tag_subject():
    p = parse_email(load("base64_note.eml"))
    assert p.subject == "Morning notes"
    [r] = p.requests
    assert r.kind == "text" and r.title == "Morning notes"
    assert r.text.startswith("Grocery thoughts and a poem draft.\n\nThe morning was grey")
    assert "naïve optimism is still optimism — and coffee" in r.text


def test_newsletter_multipart_related_prefers_html_and_strips_chrome():
    p = parse_email(load("newsletter_related.eml"))
    [r] = p.requests
    assert r.kind == "html"
    assert r.title == "The Quiet Letter #12: Libraries" and r.sender == "The Quiet Letter <letter@example.net>"
    text = em._html_visible_text(r.html)
    assert "Public libraries are one of the few places" in text
    assert "A library is a quiet argument" in text
    for junk in ("View this email", "Unsubscribe", "Manage preferences", "You are receiving", "This week: libraries"):
        assert junk not in text, junk  # last one is the hidden preheader
    assert "cid:" not in r.html and "<img" not in r.html and "<style" not in r.html


def test_unknown_8bit_body_and_raw_utf8_subject():
    p = parse_email(load("unknown_8bit.eml"))
    assert p.subject == "Café menu"
    [r] = p.requests
    assert r.text == "Café crème à la mode — “quoted” and naïve.\nSecond line."


def test_mislabeled_iso_8859_1_that_is_really_utf8():
    [r] = parse_email(load("mislabeled_charset.eml")).requests
    assert r.text == "The plan — café at 10, then the bookshop on Rüdesheimer Straße."


def test_signatures_are_stripped():
    [r] = parse_email(load("signatures.eml")).requests
    assert r.text == (
        "Wide margins make long paragraphs easier to follow, but on a small screen every millimetre counts.\n\n"
        "I think the right answer is a narrow margin and generous line spacing."
    )


def test_txt_and_html_attachments_dedupe_and_skips():
    p = parse_email(load("attachments_txt_html.eml"))
    kinds = [(r.kind, r.filename) for r in p.requests]
    # Body note dropped (short covering note), duplicate .txt deduped, image and ATT00001.txt skipped.
    assert kinds == [("text", "reading_list.txt"), ("html", "quiet-screen.html")]
    txt, html = p.requests
    assert txt.text == "Reading list\n\n1. On boredom\n2. The quiet screen — essay"  # cp1252 decoded
    assert txt.title == "reading list"
    assert html.title == ""  # the page has its own <title>
    assert "<h1>The quiet screen</h1>" in html.html
    assert all(r.sender == "Qiong Example <qiong@example.org>" for r in p.requests)


def test_text_with_url_in_the_middle_stays_text():
    [r] = parse_email(load("text_with_url.eml")).requests
    assert r.kind == "text"
    assert "https://essays.example.com/the-long-walk" in r.text
    assert r.url == ""  # the URL is mid-sentence, not the "source" of the note


def test_smime_signature_part_is_ignored():
    p = parse_email(load("smime_signed.eml"))
    [r] = p.requests
    assert r.kind == "text" and r.text.startswith("Signed notes are still notes.")


def test_thunderbird_forward_header_table():
    [r] = parse_email(load("thunderbird_forward.eml")).requests
    assert r.sender == "Erin Clerk <erin@example.com>" and r.title == "Minutes"
    assert r.text == (
        "Minutes of the reading group, 21 October. We discussed the essay on libraries and agreed to read "
        "the long walk piece next.\n\nAction: everyone brings one question."
    )


@pytest.mark.parametrize("path", sorted(p.name for p in EMAILS.glob("*.eml")))
def test_every_fixture_parses_without_crlf_or_controls(path):
    p = parse_email(load(path))
    assert p.size == len(load(path))
    for r in p.requests:
        for s in (r.text, r.html, r.title, r.sender, r.url):
            assert "\r" not in s
            assert not any(ord(c) < 32 and c not in "\n\t" for c in s)


# ------------------------------------------------------------------- built in code


def test_pdf_attachment_keeps_bytes_and_leaves_title_empty():
    pdf = tiny_pdf()
    m = msg("Fwd: the report")
    m.set_content("See attached.\n")
    m.add_attachment(pdf, maintype="application", subtype="pdf", filename="Q3 report.pdf")
    p = parse_email(raw(m))
    [r] = p.requests
    assert r.kind == "pdf" and r.data == pdf
    assert r.filename == "Q3 report.pdf" and r.content_type == "application/pdf"
    assert r.title == ""  # the PDF extractor titles it from metadata / filename
    assert r.sender == "Qiong Example <qiong@example.org>"


def test_pdf_as_octet_stream_detected_by_magic_and_named():
    pdf = tiny_pdf()
    m = msg("scan")
    m.set_content("")
    m.add_attachment(pdf, maintype="application", subtype="octet-stream")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "pdf" and r.filename == "attachment.pdf"


def test_two_identical_pdfs_and_body_with_more_text():
    pdf = tiny_pdf()
    m = msg("Two copies")
    m.set_content(
        "This is the paper we discussed on Tuesday. Please read section two before the meeting, "
        "it explains the method and why the sample was so small.\n"
    )
    m.add_attachment(pdf, maintype="application", subtype="pdf", filename="paper.pdf")
    m.add_attachment(pdf, maintype="application", subtype="pdf", filename="paper (1).pdf")
    p = parse_email(raw(m))
    assert [r.kind for r in p.requests] == ["text", "pdf"]
    assert p.requests[0].text.startswith("This is the paper we discussed")


def test_apple_inline_pdf_inside_alternative_mixed():
    """Apple Mail: alternative(plain, mixed(html, pdf, html)) - PDF must not be lost."""
    pdf = tiny_pdf()
    m = msg("Notes and slides")
    m.make_alternative()
    plain = EmailMessage()
    plain.set_content(
        "Here are the slides from the talk about reading habits, plus my notes below.\n\n"
        "The speaker argued that most people read more than they think.\n"
    )
    mixed = EmailMessage()
    mixed.make_mixed()
    h1 = EmailMessage()
    h1.set_content(
        "<p>Here are the slides from the talk about reading habits, plus my notes below.</p>", subtype="html"
    )
    att = EmailMessage()
    att.set_content(pdf, maintype="application", subtype="pdf", disposition="inline", filename="slides.pdf")
    h2 = EmailMessage()
    h2.set_content("<p>The speaker argued that most people read more than they think.</p>", subtype="html")
    for part in (h1, att, h2):
        mixed.attach(part)
    m.attach(plain)
    m.attach(mixed)
    p = parse_email(raw(m))
    kinds = [r.kind for r in p.requests]
    assert kinds == ["text", "pdf"]
    assert "most people read more" in p.requests[0].text


def test_html_only_link_share_uses_anchor_href():
    m = msg("Fwd: Interesting")
    m.set_content(
        '<div><a href="https://example.com/essays/attention">The economy of attention</a></div>'
        "<div><br></div><div>Sent from my iPad</div>",
        subtype="html",
    )
    [r] = parse_email(raw(m)).requests
    assert r.kind == "url" and r.url == "https://example.com/essays/attention"
    assert r.title == "Interesting"


def test_ios_rich_link_share_takes_first_descriptive_anchor_text():
    m = msg("")
    m.set_content(
        '<a href="https://example.com/a" class="lp-rich-link"><div class="lp-rich-link-captionBar">'
        '<a href="https://example.com/a"><b>The economy of attention</b></a>'
        '<a href="https://example.com/a">example.com</a></div></a>',
        subtype="html",
    )
    [r] = parse_email(raw(m)).requests
    assert r.kind == "url" and r.url == "https://example.com/a" and r.title == "The economy of attention"


def test_mailing_list_footer_is_stripped():
    m = msg("[reading-group] Next meeting")
    m.set_content(
        "We meet on Thursday at seven in the small room upstairs. Bring the essay and one question.\n\n"
        "_______________________________________________\n"
        "Reading-group mailing list\n"
        "reading-group@lists.example.org\n"
        "https://lists.example.org/mailman/listinfo/reading-group\n"
    )
    [r] = parse_email(raw(m)).requests
    assert r.text == "We meet on Thursday at seven in the small room upstairs. Bring the essay and one question."
    assert r.title == "Next meeting"


def test_html_only_link_share_without_subject_takes_anchor_text():
    m = msg("")
    m.set_content('<p><a href="https://example.com/a">The economy of attention</a></p>', subtype="html")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "url" and r.title == "The economy of attention"


def test_several_urls_each_become_a_request_without_titles():
    m = msg("links")
    m.set_content(
        "https://example.com/one\nhttps://example.com/two.\n<https://example.com/three>\nhttps://example.com/one\n"
    )
    p = parse_email(raw(m))
    assert [(r.kind, r.url, r.title) for r in p.requests] == [
        ("url", "https://example.com/one", ""),
        ("url", "https://example.com/two", ""),
        ("url", "https://example.com/three", ""),
    ]


def test_url_with_parentheses_and_trailing_punctuation():
    m = msg("wiki")
    m.set_content("https://en.wikipedia.org/wiki/Fish_(disambiguation).\n")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "url" and r.url == "https://en.wikipedia.org/wiki/Fish_(disambiguation)"
    # With a word of comment the line goes to the Processor as is; its link finder trims the same way.
    from sendtox3.extract.text import first_url

    m = msg("wiki")
    m.set_content("Look: https://en.wikipedia.org/wiki/Fish_(disambiguation).\n")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "auto" and first_url(r.text) == "https://en.wikipedia.org/wiki/Fish_(disambiguation)"


def test_angle_bracket_url_wrapped_over_lines_is_rejoined():
    m = msg("long link")
    m.set_content("<https://example.com/a/very/long/path/that/\n wraps/onto/the/next/line?x=1>\n")
    [r] = parse_email(raw(m)).requests
    assert r.url == "https://example.com/a/very/long/path/that/wraps/onto/the/next/line?x=1"


def test_link_in_subject_with_empty_body():
    m = msg("https://example.com/from-subject")
    m.set_content("\n")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "url" and r.url == "https://example.com/from-subject" and r.title == ""


def test_shortcut_style_url_line_plus_selected_text_is_text_with_source():
    selection = (
        "The most important part of the essay is the middle section, where the author explains why "
        "paragraph length matters for comprehension on small screens and in long reading sessions."
    )
    m = msg("[x3] The essay")
    m.set_content(f"https://example.com/essay\n\n{selection}\n")
    [r] = parse_email(raw(m)).requests
    # The Shortcut's url + text, as it would POST them: the Processor keeps the selection.
    assert r.kind == "auto" and r.url == "https://example.com/essay"
    assert selection in r.text and r.title == "The essay"


def test_short_note_without_attachments_is_kept():
    m = msg("Reminder")
    m.set_content("Buy milk and return the library book.\n")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "text" and r.text == "Buy milk and return the library book."


def test_empty_message_has_no_requests():
    m = msg("")
    m.set_content("\n\n")
    p = parse_email(raw(m))
    assert p.requests == []


def test_stub_plain_alternative_selects_html():
    m = msg("Your article")
    m.set_content("To view this email, please use an HTML-capable email client.\n")
    m.add_alternative(
        "<h1>Real heading</h1><p>Real content lives in the HTML alternative of this message, "
        "and it is long enough to be an article body on its own.</p>",
        subtype="html",
    )
    [r] = parse_email(raw(m)).requests
    assert r.kind == "html" and "Real content lives" in r.html


def test_much_longer_html_alternative_wins():
    m = msg("Long html")
    m.set_content("Short teaser.\n")
    m.add_alternative("<p>" + "A much longer paragraph in the HTML version. " * 20 + "</p>", subtype="html")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "html"


def test_equivalent_plain_alternative_wins():
    body = "Plain and HTML say the same thing, so the plain part is used because it is simpler. " * 3
    m = msg("Same")
    m.set_content(body + "\n")
    m.add_alternative(f"<p>{body}</p>", subtype="html")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "text"


def test_plain_with_link_annotations_strips_them():
    m = msg("Annotated")
    text = (
        "Read the essay <https://example.com/essay> and the follow-up <https://example.com/follow> "
        "before Friday, then tell me which argument convinced you more, which one you would cut, "
        "and why you think so after the long discussion last week."
    )
    m.set_content(text + "\n")
    m.add_alternative(f"<p>{text}</p>", subtype="html")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "text"
    assert "<https://" not in r.text
    assert r.text.startswith("Read the essay and the follow-up before Friday")


def test_plain_text_newsletter_chrome_lines_removed():
    m = msg("Digest")
    m.set_content(
        "View this email in your browser: https://example.net/v\n\n"
        "This week three essays caught our eye, all about reading slowly and paying attention to "
        "what a paragraph does.\n\n"
        "You are receiving this email because you subscribed to the digest.\n"
        "Unsubscribe: https://example.net/u\n"
    )
    [r] = parse_email(raw(m)).requests
    assert r.text == (
        "This week three essays caught our eye, all about reading slowly and paying attention to what a paragraph does."
    )


def test_unsubscribe_in_prose_is_not_removed():
    m = msg("Essay")
    sentence = (
        "Last year I decided to unsubscribe from every newsletter I had not opened in a month, and the "
        "result surprised me."
    )
    m.set_content(sentence + "\n")
    [r] = parse_email(raw(m)).requests
    assert r.text == sentence


def test_mojibake_double_encoded_utf8_is_repaired():
    m = msg("Mojibake")
    m.set_content("The cafÃ© â€” naÃ¯ve rÃ©sumÃ©.\n", charset="utf-8")
    [r] = parse_email(raw(m)).requests
    assert r.text == "The café — naïve résumé."


def test_message_rfc822_nested_titles_and_plain_body():
    inner = msg("Original note", frm="Alice Writer <alice@example.net>")
    inner.set_content(
        "An original message with enough words to be worth reading on the device, forwarded as an "
        "attachment by a desktop mail client.\n"
    )
    outer = msg("Fwd: Original note")
    outer.set_content("")
    outer.add_attachment(inner)
    p = parse_email(raw(outer))
    [r] = p.requests
    assert r.kind == "text" and r.sender == "Alice Writer <alice@example.net>" and r.title == "Original note"


def test_inline_images_and_calendar_and_vcard_are_skipped():
    m = msg("Photo")
    m.set_content(
        "Here is a picture of the reading corner and the invite for next week, which I am also adding to "
        "the calendar so nobody forgets about it.\n"
    )
    m.add_attachment(b"\x89PNG\r\n\x1a\n" + b"0" * 30, maintype="image", subtype="png", filename="corner.png")
    m.add_attachment(
        b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", maintype="text", subtype="calendar", filename="invite.ics"
    )
    m.add_attachment(b"BEGIN:VCARD\r\nEND:VCARD\r\n", maintype="text", subtype="vcard", filename="me.vcf")
    p = parse_email(raw(m))
    assert [r.kind for r in p.requests] == ["text"]


def test_unknown_binary_attachment_is_noted():
    m = msg("doc")
    m.set_content("Notes for the meeting are in the attached document, please have a look before we start.\n")
    m.add_attachment(
        b"PK\x03\x04docx",
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="notes.docx",
    )
    p = parse_email(raw(m))
    assert [r.kind for r in p.requests] == ["text"]
    assert any("notes.docx" in n for n in p.notes)


def test_attachment_filename_paths_are_stripped():
    m = msg("")
    m.set_content("")
    m.add_attachment(b"Text inside.", maintype="text", subtype="plain", filename="..\\..\\evil/notes.txt")
    [r] = parse_email(raw(m)).requests
    assert r.filename == "notes.txt"


def test_lone_text_attachment_takes_subject_as_title():
    m = msg("Meeting notes 24 Oct")
    m.set_content("")
    m.add_attachment("Point one.\nPoint two.\n".encode(), maintype="text", subtype="plain", filename="notes.txt")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "text" and r.title == "Meeting notes 24 Oct"


def test_markdown_attachment_is_text():
    m = msg("")
    m.set_content("")
    m.add_attachment(b"# Title\n\nBody.", maintype="application", subtype="octet-stream", filename="draft.md")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "text" and r.text == "# Title\n\nBody."


def test_html_attachment_with_meta_charset():
    page = '<html><head><meta charset="windows-1252"></head><body><p>Caf\xe9 \x97 page</p></body></html>'.encode(
        "latin-1"
    )
    m = msg("")
    m.set_content("")
    m.add_attachment(page, maintype="text", subtype="html", filename="page.htm")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "html" and "Café — page" in r.html and r.title == "page"


def test_malformed_input_never_raises():
    for blob in (
        b"",
        b"\x00\x01\x02",
        b"Subject: x\r\n\r\n",
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\n",
        b"From: <<<@@>>>\r\n\r\nbody",
    ):
        p = parse_email(blob)
        assert isinstance(p.requests, list)


def test_headers_only_mode_and_oversize_cap():
    p = parse_email(load("gmail_url_only.eml"), headers_only=True)
    assert p.headers_only and p.requests == [] and p.dmarc_pass and p.subject
    big = b"From: a@example.org\r\nSubject: big\r\n\r\n" + b"x" * (em.MAX_EMAIL_BYTES + 1)
    p = parse_email(big)
    assert p.headers_only and p.requests == [] and p.size == len(big) and p.notes


def test_recipient_and_envelope_addresses():
    p = parse_email(load("outlook_forward.eml"))
    assert p.to_addrs == ["x3reader+k7q2@gmail.com"]
    p = parse_email(load("gmail_url_only.eml"))
    assert p.delivered_to == ["x3reader+k7q2@gmail.com"]


# ------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Fwd: Re: [x3] Title", "Title"),
        ("[x3] Fwd: Title", "Title"),
        ("RE: FW: AW: WG: Title", "Title"),
        ("Re[2]: Title", "Title"),
        ("SV: VS: Tr: Title", "Title"),
        ("Fwd:Title", "Title"),
        ("Title (fwd)", "Title"),
        ("[x3]", ""),
        ("Regarding the plan", "Regarding the plan"),
        ("Fwdx: keep", "Fwdx: keep"),
        ("  Fwd:   spaced   out  ", "spaced out"),
        ("[EXTERNAL] [list] Re: news", "news"),
    ],
)
def test_clean_subject(subject, expected):
    assert clean_subject(subject) == expected


def test_unflow_rules():
    # soft break, quote depth change ends a paragraph, signature separator is never flowed
    src = "one \r\ntwo\r\n> q1 \r\n> q2\r\n>> deep \r\nback to zero\r\n-- \r\nsig"
    assert unflow(src) == "one two\n> q1 q2\n>> deep\nback to zero\n-- \nsig"
    assert unflow("ab \r\ncd", delsp=True) == "abcd"
    assert unflow(" stuffed line") == "stuffed line"
    assert unflow("para \r\n\r\nnext") == "para\n\nnext"  # lenient: blank line ends a flowed paragraph
    assert unflow("trailing \r\n") == "trailing\n"


@pytest.mark.parametrize(
    ("data", "declared", "expected"),
    [
        ("café".encode("utf-8"), "iso-8859-1", "café"),  # mislabelled UTF-8
        ("café".encode("cp1252"), "iso-8859-1", "café"),
        ("smart — quotes “x”".encode("cp1252"), "us-ascii", "smart — quotes “x”"),  # ascii label, cp1252 bytes
        ("café".encode("cp1252"), "unknown-8bit", "café"),
        ("café".encode("utf-8"), "unknown-8bit", "café"),
        ("café".encode("cp1252"), None, "café"),
        ("привет".encode("koi8-r"), "koi8-r", "привет"),
        ("日本語".encode("shift_jis"), "shift_jis", "日本語"),
        ("日本語".encode("iso-2022-jp"), "iso-2022-jp", "日本語"),
        (b"\xef\xbb\xbfBOM text", "iso-8859-1", "BOM text"),
        ("utf16".encode("utf-16"), None, "utf16"),
        ("x".encode(), "no-such-charset", "x"),
    ],
)
def test_decode_text_bytes(data, declared, expected):
    assert decode_text_bytes(data, declared) == expected


def _html_only(name: str) -> bytes:
    """The fixture with only its text/html part (headers kept)."""
    import email
    import email.policy

    src = email.message_from_bytes(load(name), policy=email.policy.default)
    html = src.get_body(preferencelist=("html",)).get_content()
    m = EmailMessage()
    for k in ("From", "To", "Subject", "Date", "Message-ID", "Delivered-To"):
        if src[k]:
            m[k] = str(src[k])
    for v in src.get_all("Authentication-Results") or []:
        m["Authentication-Results"] = str(v)
    m.set_content(html, subtype="html")
    return raw(m)


def test_deeply_nested_html_is_not_truncated():
    deep = "<div>" * 600 + "<p>Deep content with enough words to be a body of text for the reader.</p>" + "</div>" * 600
    m = msg("Deep")
    m.set_content(deep, subtype="html")
    [r] = parse_email(raw(m)).requests
    assert "Deep content with enough words" in em._html_visible_text(r.html)


def test_request_count_is_capped():
    m = msg("many")
    m.set_content("")
    for i in range(em.MAX_REQUESTS_PER_EMAIL + 5):
        m.add_attachment(f"text number {i}".encode(), maintype="text", subtype="plain", filename=f"f{i}.txt")
    p = parse_email(raw(m))
    assert len(p.requests) == em.MAX_REQUESTS_PER_EMAIL and any("kept the first" in n for n in p.notes)


def test_html_beyond_parser_depth_falls_back_to_text():
    deep = (
        "<div>" * 3000 + "<p>Deep content with enough words to be a body of text for the reader.</p>" + "</div>" * 3000
    )
    m = msg("Very deep")
    m.set_content(deep, subtype="html")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "text" and r.text == "Deep content with enough words to be a body of text for the reader."


def test_image_only_newsletter_yields_nothing_not_footer_junk():
    footer = (
        "You are receiving this email because you signed up on our website and agreed to receive news. "
        "Our mailing address is 1 Example Street, Example Town. "
    )
    m = msg("Sale!")
    m.set_content(
        '<table><tr><td><a href="https://shop.example/x"><img src="https://shop.example/banner.png" alt="Sale"></a></td></tr>'
        f'<tr><td style="font-size:10px"><p>{footer}</p><p><a href="https://shop.example/u">Unsubscribe</a></p></td></tr></table>',
        subtype="html",
    )
    p = parse_email(raw(m))
    assert all("receiving this email" not in (r.text + r.html) for r in p.requests)


def test_truncated_display_url_resolves_to_full_href():
    full = "https://news.example.com/2026/10/25/a-very-long-article-slug-about-attention"
    m = msg("Read this")
    m.set_content(f'<p><a href="{full}">https://news.example.com/2026/10/25/a-very-lo…</a></p>', subtype="html")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "url" and r.url == full


def test_visible_url_wins_over_tracking_href():
    m = msg("Read this")
    m.set_content(
        '<p><a href="https://click.example.net/t/abc123">https://essays.example.com/attention</a></p>', subtype="html"
    )
    [r] = parse_email(raw(m)).requests
    assert r.url == "https://essays.example.com/attention"


def test_anchor_title_with_abbreviation_is_kept():
    m = msg("")
    m.set_content('<p><a href="https://example.com/a">Dr. Strange and the long essay</a></p>', subtype="html")
    [r] = parse_email(raw(m)).requests
    assert r.title == "Dr. Strange and the long essay"


def test_epub_attachment_passes_through_as_file():
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", "<container/>")
    book = buf.getvalue()
    m = msg("A book")
    m.set_content("")
    m.add_attachment(book, maintype="application", subtype="octet-stream", filename="Moby Dick.epub")
    [r] = parse_email(raw(m)).requests
    assert r.kind == "auto" and r.data == book and r.filename == "Moby Dick.epub"
    assert r.content_type == "application/epub+zip" and r.title == ""


@pytest.mark.parametrize(
    ("name", "addr", "expected"),
    [
        ("Alice Writer", "alice@example.net", "Alice Writer <alice@example.net>"),
        ("Writer, Alice", "alice@example.net", '"Writer, Alice" <alice@example.net>'),
        ("José Müller", "jm@example.de", "José Müller <jm@example.de>"),
        ('Say "hi"', "a@example.org", '"Say \\"hi\\"" <a@example.org>'),
        ("", "a@example.org", "a@example.org"),
        ("a@example.org", "a@example.org", "a@example.org"),
        ("Only Name", "", "Only Name"),
    ],
)
def test_mailbox_round_trips_through_parseaddr(name, addr, expected):
    from email.utils import parseaddr

    value = em.mailbox(name, addr)
    assert value == expected
    if addr and name and name != addr:
        assert parseaddr(value) == (name, addr)


def test_shortcut_email_with_selection_keeps_the_selection():
    raw = (
        "From: Me <me@example.com>\r\nTo: me+x3@gmail.com\r\nSubject: [x3] Some Page\r\n"
        "Message-ID: <sel@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "https://example.com/a\r\n\r\nThe quick brown fox.\r\n"
    ).encode()
    [r] = parse_email(raw).requests
    assert (r.kind, r.url, r.text, r.title) == ("auto", "https://example.com/a", "The quick brown fox.", "Some Page")


def test_shortcut_email_with_only_a_url_is_a_link():
    raw = (
        "From: Me <me@example.com>\r\nTo: me+x3@gmail.com\r\nSubject: [x3] Some Page\r\n"
        "Message-ID: <url@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "https://example.com/a\r\n"
    ).encode()
    [r] = parse_email(raw).requests
    assert (r.kind, r.url, r.title) == ("url", "https://example.com/a", "Some Page")
