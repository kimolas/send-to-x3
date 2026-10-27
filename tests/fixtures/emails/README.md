# Email fixtures

Synthetic messages for `tests/test_email*.py` and `tests/test_imap*.py`. Every message
was written for this project; none is a copy of real mail. Names, addresses and text
are invented, and all domains are reserved example names (`example.org`,
`example.com`, `example.net`, `*.example`, `*.test`), except `gmail.com` in
recipient addresses and `aka.ms` in an Outlook footer, which are there to reproduce
real client output. License: CC0 1.0.

The files copy the MIME structure and quirks of real clients (header layout, transfer
encodings, forward banners, signature markup). `generate.py` writes them byte for byte
with CRLF line endings:

    python tests/fixtures/emails/generate.py tests/fixtures/emails

| File | Imitates | What it exercises |
|---|---|---|
| `gmail_url_only.eml` | Gmail iOS app, link share | multipart/alternative; a quoted-printable soft break inside the URL; "Sent from my iPhone"; RFC 2047 subject; Gmail `Authentication-Results` |
| `gmail_forward_inline.eml` | Gmail web forward | `---------- Forwarded message ---------` banner and header block; user signature above the banner; author signature below; `gmail_quote` / `gmail_attr` / `gmail_signature` HTML |
| `apple_forward_flowed.eml` | iOS Mail plain-text forward | `format=flowed; delsp=no` in base64; space-stuffing; quoted flowed lines; `Begin forwarded message:` |
| `apple_forward_html.eml` | macOS Mail HTML forward | banner and header lines as sibling `<div>`s inside `<blockquote type="cite">`; full plain-text alternative |
| `outlook_forward.eml` | Outlook / Microsoft 365 forward | windows-1252 quoted-printable; `________________________________` + `From:/Sent:/To:/Subject:` header block; `#divRplyFwdMsg` + `<hr>`; "Get Outlook for iOS"; Microsoft-style Authentication-Results with no authserv-id |
| `forward_as_attachment.eml` | "Forward as attachment" | `message/rfc822` part holding a newsletter; short covering note |
| `flowed_delsp_yes.eml` | DelSp=yes client | words split at soft breaks; space-stuffed lines |
| `qp_split_url_words.eml` | iOS Shortcut email | `[x3]` subject tag; URL plus a few words → link |
| `base64_note.eml` | plain note | base64 UTF-8; `Re: [x3]` subject cleanup |
| `newsletter_related.eml` | ESP newsletter | alternative(plain full of link dumps, related(HTML + `cid:` logo)); hidden preheader; "View in browser"; unsubscribe footer; tracking pixel |
| `unknown_8bit.eml` | old client | `charset=unknown-8bit` with cp1252 bytes; raw UTF-8 subject header |
| `mislabeled_charset.eml` | misconfigured client | `iso-8859-1` label on UTF-8 bytes |
| `signatures.eml` | phone + desktop signature | "Get Outlook for iOS<…>" and an RFC 3676 `-- ` signature |
| `attachments_txt_html.eml` | mixed attachments | short note; cp1252 `.txt`; `.html` sent as octet-stream; inline JPEG; duplicate `.txt`; Exchange `ATT00001.txt` stub |
| `text_with_url.eml` | note with a link in it | more than 12 words, so it stays text |
| `forged_auth_results.eml` | spoofing attempt | real topmost `dmarc=fail` above a forged lower `dmarc=pass` |
| `smime_signed.eml` | S/MIME | `multipart/signed` with `smime.p7s` |
| `thunderbird_forward.eml` | Thunderbird | `-------- Forwarded Message --------` with a tab-separated header table; format=flowed |

The tests build PDFs and some multipart layouts in code, using a tiny valid PDF made by
`tiny_pdf()` in `tests/test_email.py`.
