"""Core records: what arrives (IngestRequest) and what is queued (Item)."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

IngestKind = Literal["auto", "url", "text", "html", "pdf"]
IngestSource = Literal["http", "email", "ha_share", "admin", "test"]
ItemKind = Literal["url", "text", "email", "pdf"]
Channel = Literal["catalog", "auto", "opds", "push"]
CHANNELS: tuple[str, ...] = ("catalog", "auto", "opds", "push")

_B32 = "abcdefghijklmnopqrstuvwxyz234567"


def new_item_id() -> str:
    """10 chars of lowercase base32: URL-safe, filename-safe, no template braces."""
    return "".join(secrets.choice(_B32) for _ in range(10))


@dataclass
class IngestRequest:
    """One thing to turn into an Item. Produced by every ingest channel."""

    kind: IngestKind = "auto"
    source: IngestSource = "http"
    url: str = ""
    text: str = ""
    html: str = ""
    title: str = ""
    data: Optional[bytes] = None  # file payload (PDF, .txt, .html attachment)
    filename: str = ""
    content_type: str = ""
    sender: str = ""  # email From / HA device name, used as byline fallback
    received_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class Item:
    id: str
    created_at: int
    kind: ItemKind
    source: str
    title: str
    byline: str = ""
    site: str = ""
    source_url: str = ""
    # "failed" = extraction failed; a fallback document (title, URL, error note) was still
    # rendered, so the item is deliverable and the user sees what went wrong on the X3.
    status: Literal["ready", "failed"] = "ready"
    error: str = ""
    word_count: int = 0
    dedupe_key: str = ""
    delivered: dict[str, int] = field(default_factory=dict)  # channel -> unix ts

    @property
    def is_pending(self) -> bool:
        """Not yet delivered through any channel (failed items deliver their fallback)."""
        return not self.delivered
