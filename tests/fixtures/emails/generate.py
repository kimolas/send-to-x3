"""Generate the synthetic .eml corpus in tests/fixtures/emails (all content invented, CC0).

Run from the repo root: python tests/fixtures/emails/generate.py tests/fixtures/emails
Messages are written byte-exact with CRLF line endings, mimicking the structure of
real Gmail / Apple Mail / Outlook / newsletter mail.
"""

from __future__ import annotations

import base64
import quopri
import sys
from pathlib import Path

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

GMAIL_AR = (
    "Authentication-Results: mx.google.com;\r\n"
    "       dkim=pass header.i=@example.org header.s=s2026 header.b=AbCdEf12;\r\n"
    "       spf=pass (google.com: domain of qiong@example.org designates 192.0.2.10 as permitted sender) smtp.mailfrom=qiong@example.org;\r\n"
    "       dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.org\r\n"
)
COMMON = (
    "Delivered-To: x3reader+k7q2@gmail.com\r\n"
    "Received: by 2002:a05:6000:1:b0:1 with SMTP id x; Sun, 25 Oct 2026 06:00:01 -0700 (PDT)\r\n"
    + GMAIL_AR
    + "MIME-Version: 1.0\r\n"
    "Date: Sun, 25 Oct 2026 09:00:00 -0400\r\n"
)


def crlf(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\n", "\r\n")


def qp(s: str, charset: str = "utf-8") -> str:
    """Quoted-printable with LF line ends (write() turns them into CRLF)."""
    return quopri.encodestring(s.replace("\r\n", "\n").encode(charset)).decode("ascii")


def b64(b: bytes) -> str:
    e = base64.b64encode(b).decode("ascii")
    return "\r\n".join(e[i : i + 76] for i in range(0, len(e), 76))


def write(name: str, headers: str, body: str | bytes) -> None:
    data = crlf(headers).encode("utf-8") + b"\r\n" + (body if isinstance(body, bytes) else crlf(body).encode("utf-8"))
    (OUT / name).write_bytes(data)


# 1. Gmail (iOS app) URL-only share: QP splits the long URL across soft line breaks.
url = "https://news.example.com/2026/10/25/a-very-long-article-slug-about-reading-on-e-ink-devices?utm_source=share&utm_medium=ios_app&ref=readlater"
write(
    "gmail_url_only.eml",
    COMMON
    + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: =?UTF-8?Q?Reading_on_e-ink_=E2=80=94_a_long_read?=\n"
    'Message-ID: <CAF0001@mail.gmail.com>\nContent-Type: multipart/alternative; boundary="000000000000a1b2"\n',
    "--000000000000a1b2\n"
    'Content-Type: text/plain; charset="UTF-8"\nContent-Transfer-Encoding: quoted-printable\n\n'
    + qp(url + "\n\nSent from my iPhone\n")
    + "\n--000000000000a1b2\n"
    'Content-Type: text/html; charset="UTF-8"\nContent-Transfer-Encoding: quoted-printable\n\n'
    + qp(
        f'<div dir="ltr"><a href="{url.replace("&", "&amp;")}">{url.replace("&", "&amp;")}</a><br><br><div dir="ltr">Sent from my iPhone</div></div>\n'
    )
    + "\n--000000000000a1b2--\n",
)

# 2. Gmail web forward: user signature above the forward, original author's signature below.
plain2 = """Thought you'd like this.

--
Qiong Example
Product Notes Weekly

---------- Forwarded message ---------
From: Alice Writer <alice@example.net>
Date: Fri, Oct 23, 2026 at 5:12 PM
Subject: Notes from the café
To: Qiong Example <qiong@example.org>


Here are my notes from Thursday’s session at the café. We talked about how
long-form reading works on small screens, and why paragraphs matter more than
pictures for this kind of device.

The second point was about naïve layout engines: they re-measure every line,
so very long paragraphs get slow. Short paragraphs are kinder.

--
Alice Writer
Senior Researcher | Example Labs
"""
html2 = """<div dir="ltr">Thought you'd like this.<br clear="all"><br><span class="gmail_signature_prefix">-- </span><br><div dir="ltr" class="gmail_signature" data-smartmail="gmail_signature">Qiong Example<br>Product Notes Weekly</div><br><div class="gmail_quote"><div dir="ltr" class="gmail_attr">---------- Forwarded message ---------<br>From: <strong class="gmail_sendername" dir="auto">Alice Writer</strong> <span dir="auto">&lt;<a href="mailto:alice@example.net">alice@example.net</a>&gt;</span><br>Date: Fri, Oct 23, 2026 at 5:12 PM<br>Subject: Notes from the café<br>To: Qiong Example &lt;<a href="mailto:qiong@example.org">qiong@example.org</a>&gt;<br></div><br><br><div dir="ltr"><p>Here are my notes from Thursday’s session at the café. We talked about how long-form reading works on small screens, and why paragraphs matter more than pictures for this kind of device.</p><p>The second point was about naïve layout engines: they re-measure every line, so very long paragraphs get slow. Short paragraphs are kinder.</p><p>-- <br>Alice Writer<br>Senior Researcher | Example Labs</p></div></div></div>
"""
write(
    "gmail_forward_inline.eml",
    COMMON
    + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Fwd: Notes from the =?UTF-8?Q?caf=C3=A9?=\n"
    'Message-ID: <CAF0002@mail.gmail.com>\nContent-Type: multipart/alternative; boundary="000000000000c3d4"\n',
    '--000000000000c3d4\nContent-Type: text/plain; charset="UTF-8"\nContent-Transfer-Encoding: quoted-printable\n\n'
    + qp(plain2)
    + '\n--000000000000c3d4\nContent-Type: text/html; charset="UTF-8"\nContent-Transfer-Encoding: quoted-printable\n\n'
    + qp(html2)
    + "\n--000000000000c3d4--\n",
)

# 3. Apple Mail (iOS) plain forward, format=flowed delsp=no, base64, "Sent from my iPhone".
flowed3 = (
    "Worth a read on the train.\r\n\r\nSent from my iPhone\r\n\r\nBegin forwarded message:\r\n\r\n"
    "From: Bob Author <bob@example.com>\r\nSubject: Long thoughts\r\n"
    "Date: October 22, 2026 at 8:01:22 PM EDT\r\nTo: Qiong Example <qiong@example.org>\r\n\r\n"
    "This is a long paragraph that the mail client wrapped at seventy-two \r\n"
    "characters using format=flowed soft breaks, which a naive reader would \r\n"
    "show as ragged short lines.\r\n\r\n"
    "> quoted line from an earlier \r\n> message that also flows\r\n\r\n"
    " From the space-stuffed line onwards, the second paragraph \r\ncontinues here.\r\n"
)
write(
    "apple_forward_flowed.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Fwd: Long thoughts\n"
    "Message-ID: <A1B2C3D4-0003@example.org>\nContent-Type: text/plain; charset=utf-8; format=flowed; delsp=no\n"
    "Content-Transfer-Encoding: base64\nX-Mailer: iPhone Mail (23A341)\n",
    b64(flowed3.encode("utf-8")) + "\n",
)

# 4. Apple Mail (macOS) HTML forward: banner + header <div>s inside <blockquote type="cite">.
html4 = """<html><head><meta http-equiv="content-type" content="text/html; charset=utf-8"></head><body style="overflow-wrap: break-word;"><div dir="auto"><br></div><div dir="auto"><div><br><blockquote type="cite"><div>Begin forwarded message:</div><br class="Apple-interchange-newline"><div style="margin-top: 0px; margin-right: 0px; margin-bottom: 0px; margin-left: 0px;"><span style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, sans-serif; color:rgba(0, 0, 0, 1.0);"><b>From: </b></span><span style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, sans-serif;">Dana Essayist &lt;<a href="mailto:dana@example.com">dana@example.com</a>&gt;<br></span></div><div style="margin-top: 0px; margin-right: 0px; margin-bottom: 0px; margin-left: 0px;"><span style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, sans-serif; color:rgba(0, 0, 0, 1.0);"><b>Subject: </b></span><span style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, sans-serif;"><b>On slow reading</b><br></span></div><div style="margin-top: 0px; margin-right: 0px; margin-bottom: 0px; margin-left: 0px;"><span style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, sans-serif; color:rgba(0, 0, 0, 1.0);"><b>Date: </b></span><span style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, sans-serif;">October 21, 2026 at 7:45:10 AM EDT<br></span></div><div style="margin-top: 0px; margin-right: 0px; margin-bottom: 0px; margin-left: 0px;"><span style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, sans-serif; color:rgba(0, 0, 0, 1.0);"><b>To: </b></span><span style="font-family: -webkit-system-font, Helvetica Neue, Helvetica, sans-serif;">Qiong Example &lt;<a href="mailto:qiong@example.org">qiong@example.org</a>&gt;<br></span></div><br><div><div><h2>Why slow reading matters</h2><p>Slow reading is a practice of attention. An e-ink screen helps because nothing moves, nothing blinks, and nothing asks for a tap.</p><p>Try one long essay a week, on paper or on an e-reader, without notifications.</p></div></div></blockquote></div><br></div></body></html>
"""
write(
    "apple_forward_html.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Fwd: On slow reading\n"
    'Message-ID: <A1B2C3D4-0004@example.org>\nContent-Type: multipart/alternative; boundary="Apple-Mail=_AAAA-0004"\n'
    "X-Mailer: Apple Mail (2.3826.700.81)\n",
    "--Apple-Mail=_AAAA-0004\nContent-Transfer-Encoding: 7bit\nContent-Type: text/plain;\n\tcharset=us-ascii\n\n"
    "\n\nBegin forwarded message:\n\nFrom: Dana Essayist <dana@example.com>\nSubject: On slow reading\n"
    "Date: October 21, 2026 at 7:45:10 AM EDT\nTo: Qiong Example <qiong@example.org>\n\n"
    "Why slow reading matters\n\nSlow reading is a practice of attention. An e-ink screen helps because nothing moves, nothing blinks, and nothing asks for a tap.\n\n"
    "Try one long essay a week, on paper or on an e-reader, without notifications.\n\n"
    "--Apple-Mail=_AAAA-0004\nContent-Transfer-Encoding: quoted-printable\nContent-Type: text/html;\n\tcharset=utf-8\n\n"
    + qp(html4)
    + "\n--Apple-Mail=_AAAA-0004--\n",
)

# 5. Outlook (Microsoft 365) forward, windows-1252 QP, no Gmail A-R (Microsoft style A-R).
plain5 = """FYI

________________________________
From: Carol Planner <carol@example.com>
Sent: Thursday, October 22, 2026 10:00 AM
To: Qiong Example <qiong@example.org>
Subject: Plan for the reading group

Hello all,

The plan for next week — we meet at the café at 10. Bring the essay you liked most this month and one question about it.

Thanks,
Carol

Get Outlook for iOS<https://aka.ms/o0ukef>
"""
html5 = """<html><head><meta http-equiv="Content-Type" content="text/html; charset=Windows-1252"><style type="text/css" style="display:none;"> P {margin-top:0;margin-bottom:0;} </style></head><body dir="ltr"><div class="elementToProof" style="font-family: Aptos, Calibri, Helvetica, sans-serif; font-size: 12pt;">FYI</div><div id="appendonsend"></div><hr style="display:inline-block;width:98%" tabindex="-1"><div id="divRplyFwdMsg" dir="ltr"><font face="Calibri, sans-serif" style="font-size:11pt" color="#000000"><b>From:</b> Carol Planner &lt;carol@example.com&gt;<br><b>Sent:</b> Thursday, October 22, 2026 10:00 AM<br><b>To:</b> Qiong Example &lt;qiong@example.org&gt;<br><b>Subject:</b> Plan for the reading group</font><div>&nbsp;</div></div><div dir="ltr"><p>Hello all,</p><p>The plan for next week — we meet at the café at 10. Bring the essay you liked most this month and one question about it.</p><p>Thanks,<br>Carol</p><div id="ms-outlook-mobile-signature">Get <a href="https://aka.ms/o0ukef">Outlook for iOS</a></div></div></body></html>
"""
write(
    "outlook_forward.eml",
    "Received: from EXAMPLE01.prod.outlook.com (2603:10b6::1) by mx.example.org; Thu, 22 Oct 2026 14:05:00 +0000\n"
    "Authentication-Results: spf=pass (sender IP is 192.0.2.44) smtp.mailfrom=example.org; dkim=pass (signature was verified) header.d=example.org;dmarc=pass action=none header.from=example.org;compauth=pass reason=100\n"
    "MIME-Version: 1.0\nDate: Thu, 22 Oct 2026 10:05:00 -0400\n"
    'From: Qiong Example <qiong@example.org>\nTo: "x3 reader" <x3reader+k7q2@gmail.com>\nSubject: FW: Plan for the reading group\n'
    'Message-ID: <PH0PR00MB0005@PH0PR00MB0005.namprd00.prod.outlook.com>\nContent-Type: multipart/alternative; boundary="_000_PH0PR00MB0005_"\n',
    '--_000_PH0PR00MB0005_\nContent-Type: text/plain; charset="Windows-1252"\nContent-Transfer-Encoding: quoted-printable\n\n'
    + qp(plain5, "cp1252")
    + '\n--_000_PH0PR00MB0005_\nContent-Type: text/html; charset="Windows-1252"\nContent-Transfer-Encoding: quoted-printable\n\n'
    + qp(html5, "cp1252")
    + "\n--_000_PH0PR00MB0005_--\n",
)

# 6. Forwarded as attachment (message/rfc822) of a newsletter; outer body is a short note.
write(
    "forward_as_attachment.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Fwd: Weekly digest\n"
    'Message-ID: <fwd-attach-0006@example.org>\nContent-Type: multipart/mixed; boundary="XYZ0006"\n',
    "--XYZ0006\nContent-Type: text/plain; charset=us-ascii\n\nsee attached\n\n"
    '--XYZ0006\nContent-Type: message/rfc822\nContent-Disposition: attachment; filename="Weekly digest.eml"\n\n'
    "From: Example Newsletter <news@example.net>\nTo: qiong@example.org\nSubject: Weekly digest #42\n"
    "Date: Sat, 24 Oct 2026 08:00:00 +0000\nMIME-Version: 1.0\nContent-Type: text/html; charset=utf-8\n\n"
    '<html><body><table><tr><td><a href="https://example.net/view?utm_source=newsletter&amp;utm_medium=email">View in browser</a></td></tr>\n'
    "<tr><td><h1>Weekly digest</h1><p>This week we look at three essays about attention, each with a short summary and a link to the full text.</p>\n"
    "<ul><li>On boredom, by an anonymous reader</li><li>The quiet screen</li><li>Paragraphs as places</li></ul>"
    '<p>Read more: <a href="https://news.example.com/story?id=7&amp;utm_campaign=x&amp;fbclid=IwAR0">the full story</a></p>\n'
    '<img src="https://example.net/pixel.gif?uid=1" width="1" height="1"></td></tr>\n'
    '<tr><td style="font-size:10px">You received this because you subscribed. <a href="https://example.net/unsub">Unsubscribe</a></td></tr></table></body></html>\n\n'
    "--XYZ0006--\n",
)

# 7. format=flowed with delsp=yes (soft breaks inside words are joined without a space).
flowed7 = (
    "Supercalifragilistic words get split by DelSp=Yes clients: extraordin \r\n"
    "arily long words survive. A second sentence flows  \r\n"
    "onto the next line, and a line that starts with a space is \r\n"
    "  space-stuffed.\r\n\r\nNew paragraph.\r\n"
)
write(
    "flowed_delsp_yes.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Flowed delsp\n"
    'Message-ID: <flowed-0007@example.org>\nContent-Type: text/plain; charset="utf-8"; format="flowed"; delsp="yes"\n'
    "Content-Transfer-Encoding: 8bit\n",
    flowed7,
)

# 8. Quoted-printable URL with a few words: still a link share.
url8 = "https://blog.example.com/posts/2026/why-e-ink-is-good-for-long-form-reading-and-other-notes.html"
write(
    "qp_split_url_words.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: [x3] Why e-ink is good\n"
    "Message-ID: <qp-0008@example.org>\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: quoted-printable\n",
    qp(f"Check this out, it's great for the weekend:\n{url8}\n"),
)

# 9. base64 UTF-8 note (a real text body, several paragraphs, no URL).
note9 = (
    "Grocery thoughts and a poem draft.\n\nThe morning was grey and the café on the corner had already "
    "put its chairs out. I wrote three lines and crossed out two.\n\nRemember: naïve optimism is still "
    "optimism — and coffee is cheaper at home.\n"
)
write(
    "base64_note.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Re: [x3] Morning notes\n"
    "Message-ID: <b64-0009@example.org>\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n",
    b64(note9.encode("utf-8")) + "\n",
)

# 10. Newsletter: alternative(plain link-dump, related(html + cid image)), preheader, chrome.
plain10 = """View this email in your browser [https://example.net/v/1?utm_source=nl]

THE QUIET LETTER [https://example.net/?utm_source=nl]

Issue 12 [https://example.net/12?utm_source=nl] - essays [https://example.net/essays] and links [https://example.net/links]

A long essay about libraries.

Unsubscribe [https://example.net/u/1] | Manage preferences [https://example.net/p/1]
"""
html10 = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>The Quiet Letter</title><style>.mobile{display:none}</style></head>
<body><div style="display:none;max-height:0;overflow:hidden;mso-hide:all">This week: libraries, silence and the long walk home &zwnj;&nbsp;&zwnj;&nbsp;</div>
<table width="100%"><tr><td align="center"><a href="https://example.net/v/1?utm_source=nl">View this email in your browser</a></td></tr>
<tr><td><img src="cid:logo@example.net" alt="The Quiet Letter" width="200"></td></tr>
<tr><td><h1>The Quiet Letter</h1><h2>Issue 12: Libraries</h2>
<p>Public libraries are one of the few places where you can stay as long as you like without buying anything. This issue is about what that does for reading.</p>
<p>We visited three small branch libraries and asked the staff what people read when nobody is watching. The answers were surprising: poetry, repair manuals, and long novels.</p>
<blockquote>A library is a quiet argument that time spent reading is time well spent.</blockquote>
<p>Next week: walking and thinking.</p></td></tr>
<tr><td style="font-size:11px;color:#888">You are receiving this email because you signed up at example.net.<br><a href="https://example.net/u/1">Unsubscribe</a> | <a href="https://example.net/p/1">Manage preferences</a></td></tr>
<tr><td><img src="https://example.net/open.gif?u=1" width="1" height="1" alt=""></td></tr></table></body></html>
"""
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
write(
    "newsletter_related.eml",
    "Delivered-To: x3reader+k7q2@gmail.com\n"
    "Authentication-Results: mx.google.com;\n       dkim=pass header.i=@mail.example.net header.s=nl header.b=Zz99;\n"
    "       spf=pass (google.com: domain of bounce@mail.example.net designates 192.0.2.20 as permitted sender) smtp.mailfrom=bounce@mail.example.net;\n"
    "       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.net\n"
    "MIME-Version: 1.0\nDate: Sat, 24 Oct 2026 12:00:00 +0000\n"
    "From: The Quiet Letter <letter@example.net>\nTo: x3reader+k7q2@gmail.com\nSubject: The Quiet Letter #12: Libraries\n"
    "List-Unsubscribe: <https://example.net/u/1>\n"
    'Message-ID: <nl-0010@mail.example.net>\nContent-Type: multipart/alternative; boundary="ALT0010"\n',
    "--ALT0010\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit\n\n"
    + plain10
    + '\n--ALT0010\nContent-Type: multipart/related; boundary="REL0010"; type="text/html"\n\n'
    "--REL0010\nContent-Type: text/html; charset=utf-8\nContent-Transfer-Encoding: quoted-printable\n\n"
    + qp(html10)
    + '\n--REL0010\nContent-Type: image/png; name="logo.png"\nContent-ID: <logo@example.net>\n'
    'Content-Disposition: inline; filename="logo.png"\nContent-Transfer-Encoding: base64\n\n'
    + b64(png)
    + "\n--REL0010--\n\n--ALT0010--\n",
)

# 12. unknown-8bit (cp1252 bytes) body and a raw UTF-8 subject.
write_raw = (
    crlf(COMMON + "From: Old Client <old@example.org>\nTo: x3reader+k7q2@gmail.com\n").encode("utf-8")
    + "Subject: Café menu\r\n".encode("utf-8")
    + b"Message-ID: <u8bit-0012@example.org>\r\nContent-Type: text/plain; charset=unknown-8bit\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n\r\n"
    + "Café crème à la mode — “quoted” and naïve.\r\nSecond line.\r\n".encode("cp1252")
)
(OUT / "unknown_8bit.eml").write_bytes(write_raw)

# 13. Mislabelled charset: iso-8859-1 label on UTF-8 bytes (8bit).
write_raw = (
    crlf(
        COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Mislabelled\n"
    ).encode("utf-8")
    + b"Message-ID: <mislabel-0013@example.org>\r\nContent-Type: text/plain; charset=iso-8859-1\r\n"
    b"Content-Transfer-Encoding: 8bit\r\n\r\n"
    + "The plan — café at 10, then the bookshop on Rüdesheimer Straße.\r\n".encode("utf-8")
)
(OUT / "mislabeled_charset.eml").write_bytes(write_raw)

# 14. Signatures: RFC 3676 separator and an Outlook mobile footer.
write(
    "signatures.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Thoughts on margins\n"
    "Message-ID: <sig-0014@example.org>\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit\n",
    "Wide margins make long paragraphs easier to follow, but on a small screen every millimetre counts.\n\n"
    "I think the right answer is a narrow margin and generous line spacing.\n\n"
    "Get Outlook for iOS<https://aka.ms/o0ukef>\n\n-- \nQiong Example\n+1 555 0100\nhttps://example.org\n",
)

# 15. Attachments: short note + .txt (cp1252) + .html + inline image + duplicate .txt + Exchange stub.
txt = "Reading list\r\n\r\n1. On boredom\r\n2. The quiet screen — essay\r\n".encode("cp1252")
html_att = (
    "<html><head><title>Saved page: the quiet screen</title></head><body><h1>The quiet screen</h1>"
    "<p>An essay saved as a web page, about screens that stay still.</p></body></html>"
).encode("utf-8")
write(
    "attachments_txt_html.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Some files\n"
    'Message-ID: <att-0015@example.org>\nContent-Type: multipart/mixed; boundary="MIX0015"\n',
    "--MIX0015\nContent-Type: text/plain; charset=utf-8\n\nHere are the files.\n\n"
    '--MIX0015\nContent-Type: text/plain; charset=windows-1252; name="reading_list.txt"\n'
    'Content-Disposition: attachment; filename="reading_list.txt"\nContent-Transfer-Encoding: base64\n\n'
    + b64(txt)
    + '\n--MIX0015\nContent-Type: application/octet-stream; name="quiet-screen.html"\n'
    'Content-Disposition: attachment; filename="quiet-screen.html"\nContent-Transfer-Encoding: base64\n\n'
    + b64(html_att)
    + '\n--MIX0015\nContent-Type: image/jpeg; name="IMG_0001.jpg"\nContent-Disposition: inline; filename="IMG_0001.jpg"\n'
    "Content-Transfer-Encoding: base64\n\n/9j/4AAQSkZJRgABAQAAAQABAAD/2Q==\n"
    '--MIX0015\nContent-Type: text/plain; charset=windows-1252; name="reading_list copy.txt"\n'
    'Content-Disposition: attachment; filename="reading_list copy.txt"\nContent-Transfer-Encoding: base64\n\n'
    + b64(txt)
    + '\n--MIX0015\nContent-Type: text/plain; charset=us-ascii; name="ATT00001.txt"\n'
    'Content-Disposition: attachment; filename="ATT00001.txt"\n\n_______________________________________________\nList footer\n'
    "--MIX0015--\n",
)

# 16. A real note that contains a URL in the middle (more than 12 other words): text.
write(
    "text_with_url.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Train reading plan\n"
    "Message-ID: <txturl-0016@example.org>\nContent-Type: text/plain; charset=utf-8\n",
    "For the train tomorrow I want to finish the essay on libraries, then start the long piece at "
    "https://essays.example.com/the-long-walk (it is about forty minutes of reading), and finally "
    "write down three questions for the reading group.\n",
)

# 17. Forged lower Authentication-Results: the real (topmost) one says dmarc=fail.
write(
    "forged_auth_results.eml",
    "Delivered-To: x3reader+k7q2@gmail.com\n"
    "Authentication-Results: mx.google.com;\n       spf=fail (google.com: domain of qiong@example.org does not designate 203.0.113.9 as permitted sender) smtp.mailfrom=qiong@example.org;\n"
    "       dmarc=fail (p=REJECT sp=REJECT dis=QUARANTINE) header.from=example.org\n"
    "Received: from evil.example (evil.example [203.0.113.9]) by mx.google.com; Sun, 25 Oct 2026 06:00:00 -0700\n"
    "Authentication-Results: mx.google.com; dkim=pass header.i=@example.org; dmarc=pass header.from=example.org\n"
    "MIME-Version: 1.0\nDate: Sun, 25 Oct 2026 09:00:00 -0400\n"
    "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Totally legit\n"
    "Message-ID: <forged-0017@evil.example>\nContent-Type: text/plain; charset=utf-8\n",
    "Please read https://phish.example/login now.\n",
)

# 18. S/MIME signed message: the signature part must be ignored.
write(
    "smime_signed.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Signed note\n"
    'Message-ID: <smime-0018@example.org>\nContent-Type: multipart/signed; protocol="application/pkcs7-signature"; micalg=sha-256; boundary="SIG0018"\n',
    "This is a cryptographically signed message in MIME format.\n\n--SIG0018\n"
    "Content-Type: text/plain; charset=utf-8\n\nSigned notes are still notes. This one is about keeping a reading journal every evening.\n\n"
    '--SIG0018\nContent-Type: application/pkcs7-signature; name="smime.p7s"\nContent-Disposition: attachment; filename="smime.p7s"\n'
    "Content-Transfer-Encoding: base64\n\nMIAGCSqGSIb3DQEHAqCAMIACAQExDzANBglghkgBZQMEAgEFADCABgkqhkiG9w0BBwEAAKCAMIIB\n\n"
    "--SIG0018--\n",
)

# 19. Thunderbird inline forward with a header table in plain text.
write(
    "thunderbird_forward.eml",
    COMMON + "From: Qiong Example <qiong@example.org>\nTo: x3reader+k7q2@gmail.com\nSubject: Fwd: Minutes\n"
    "Message-ID: <tb-0019@example.org>\nContent-Type: text/plain; charset=UTF-8; format=flowed\nContent-Transfer-Encoding: 8bit\n"
    "User-Agent: Mozilla Thunderbird\n",
    "\n\n\n-------- Forwarded Message --------\nSubject: \tMinutes\nDate: \tWed, 21 Oct 2026 18:00:00 +0200\n"
    "From: \tErin Clerk <erin@example.com>\nTo: \treading-group@example.com\n\n\n\n"
    "Minutes of the reading group, 21 October. We discussed the essay on \nlibraries and agreed to read the long walk piece next.\n\n"
    "Action: everyone brings one question.\n",
)
print("wrote", len(list(OUT.glob("*.eml"))), "files to", OUT)
