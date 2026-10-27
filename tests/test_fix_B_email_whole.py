"""Finding 0 end to end: an email newsletter keeps its lists in the stored TXT.

Real parse_email -> Processor -> extract -> render (no fakes). Relies on ingest passing
``whole=True`` to :func:`sendtox3.extract.html.html_to_document` for email HTML bodies
(the shared interface decision between the ingest and extract owners).
"""

from __future__ import annotations

from pathlib import Path

from sendtox3.extract.email import parse_email
from sendtox3.ingest import Processor

EMAILS = Path(__file__).parent / "fixtures" / "emails"


async def test_forwarded_newsletter_list_items_reach_the_txt(store, opts):
    parsed = parse_email((EMAILS / "forward_as_attachment.eml").read_bytes())
    [req] = [r for r in parsed.requests if r.html]
    assert req.source == "email"
    item = await Processor(store, opts).process(req)
    assert item.status == "ready"
    txt = store.file_bytes(item.id, "txt").decode("utf-8")
    for line in ("On boredom, by an anonymous reader", "The quiet screen", "Paragraphs as places"):
        assert line in txt
    assert "three essays about attention" in txt
    assert "could be extracted" not in txt and "JavaScript" not in txt
