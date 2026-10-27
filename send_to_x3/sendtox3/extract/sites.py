"""Site APIs for pages that extract badly (or not at all) from their HTML.

Each handler turns a shared URL into a Document using a public, unauthenticated API:

- GitHub repository README (``api.github.com/repos/{o}/{r}/readme``, rendered HTML):
  the github.com page yields no headings.
- Wikipedia (``w/api.php`` TextExtracts, plain text with ``== Heading ==`` lines):
  no navboxes, ``[edit]`` links, citation markers or reference lists.
- Substack (``/api/v1/posts/{slug}``): clean ``body_html`` with every heading kept.
- Bluesky (``public.api.bsky.app`` getPostThread): the web app is JavaScript-only.
- X / Twitter (``publish.twitter.com/oembed``): x.com is JavaScript-only.

:func:`site_document` returns ``None`` when the URL is not handled or the API gave
nothing usable; the caller then fetches the page itself. Handlers never raise for
remote problems. The ``fetch`` callable is injected (it is
:func:`~sendtox3.extract.fetch.fetch_url` with the caller's timeout and SSRF policy).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qs, quote, unquote, urlsplit

from lxml import html as lxml_html

from ..textmodel import Block, Document, Heading, Paragraph, Quote, clean_text
from .fetch import FetchError, FetchResult
from .html import clean_author, html_fragment_to_blocks
from .text import derive_title, drop_empty_sections, guess_lang, normalize_heading_levels, text_to_blocks

__all__ = ["Fetcher", "SiteMatch", "match_site", "site_document"]

log = logging.getLogger(__name__)


class Fetcher(Protocol):
    """``fetch_url`` bound to the caller's timeout and SSRF policy."""

    def __call__(self, url: str, /, *, headers: dict[str, str] | None = None) -> Awaitable[FetchResult]: ...


@dataclass(frozen=True)
class SiteMatch:
    """A URL recognised as belonging to a site with an API."""

    site: str  # "github" | "wikipedia" | "substack" | "bluesky" | "x"
    params: dict[str, str] = field(default_factory=dict)


_Handler = Callable[[SiteMatch, str, Fetcher], Awaitable["Document | None"]]


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------

_GH_RESERVED = frozenset(
    """about account apps blog collections contact customer-stories enterprise explore features
    issues login logout marketplace new notifications orgs organizations pricing pulls search
    security settings site sponsors topics trending users join codespaces copilot""".split()
)
_GH_DOC_EXT = (".md", ".markdown", ".mdown", ".rst", ".txt", ".adoc", ".asciidoc", ".org", ".textile")
_WIKI_HOST_RE = re.compile(r"^(?P<lang>[a-z][a-z0-9-]{1,15})\.(?:m\.)?wikipedia\.org$")
_WIKI_NAMESPACES = frozenset(
    """special file image category template help portal talk user wikipedia draft module
    mediawiki media timedtext book education_program gadget topic""".split()
)
_SUBSTACK_HOST_RE = re.compile(r"^(?P<sub>[a-z0-9-]+)\.substack\.com$")
_X_HOSTS = frozenset(
    {
        "x.com",
        "twitter.com",
        "mobile.twitter.com",
        "mobile.x.com",
        "fxtwitter.com",
        "vxtwitter.com",
        "fixupx.com",
        "fixvx.com",
    }
)
_STATUS_RE = re.compile(r"^/(?P<user>[A-Za-z0-9_]{1,30}|i(?:/web)?)/status(?:es)?/(?P<id>\d{1,25})(?:/|$)")
_BSKY_RE = re.compile(r"^/profile/(?P<actor>[^/]+)/post/(?P<rkey>[A-Za-z0-9._~:-]+)/?$")


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def match_site(url: str) -> SiteMatch | None:
    """Recognise URLs handled by a site API (no network)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    host = _host(url)
    segs = [unquote(s) for s in p.path.split("/") if s]

    if host == "github.com" and len(segs) >= 2 and segs[0].lower() not in _GH_RESERVED:
        owner, repo = segs[0], re.sub(r"\.git$", "", segs[1])
        if len(segs) == 2:
            return SiteMatch("github", {"owner": owner, "repo": repo, "kind": "readme"})
        if segs[2] == "tree" and len(segs) >= 4:
            return SiteMatch(
                "github", {"owner": owner, "repo": repo, "kind": "readme", "ref": segs[3], "dir": "/".join(segs[4:])}
            )
        if segs[2] == "blob" and len(segs) >= 5 and segs[-1].lower().endswith(_GH_DOC_EXT):
            return SiteMatch(
                "github", {"owner": owner, "repo": repo, "kind": "file", "ref": segs[3], "path": "/".join(segs[4:])}
            )
        return None

    m = _WIKI_HOST_RE.match(host)
    if m:
        title = ""
        if p.path.startswith("/wiki/"):
            title = unquote(p.path[len("/wiki/") :])
        elif p.path in ("/w/index.php", "/w/index.php/"):
            title = (parse_qs(p.query).get("title") or [""])[0]
            if any(k in parse_qs(p.query) for k in ("action", "diff", "oldid")):
                return None
        title = title.replace("_", " ").strip()
        ns = title.split(":", 1)[0].strip().lower() if ":" in title else ""
        if not title or ns in _WIKI_NAMESPACES or ns.endswith(" talk"):
            return None
        return SiteMatch("wikipedia", {"lang": m.group("lang"), "title": title})

    m = _SUBSTACK_HOST_RE.match(host)
    if m and m.group("sub") not in ("open", "www", "substack") and len(segs) == 2 and segs[0] == "p":
        return SiteMatch("substack", {"base": f"https://{host}", "slug": segs[1]})
    if host == "open.substack.com" and len(segs) == 4 and segs[0] == "pub" and segs[2] == "p":
        return SiteMatch("substack", {"base": f"https://{segs[1].lower()}.substack.com", "slug": segs[3]})

    if host in ("bsky.app", "main.bsky.dev"):
        m = _BSKY_RE.match(p.path)
        if m:
            return SiteMatch("bluesky", {"actor": unquote(m.group("actor")), "rkey": m.group("rkey")})
        return None

    if host in _X_HOSTS:
        m = _STATUS_RE.match(p.path)
        if m:
            user = "i" if m.group("user") in ("i", "i/web") else m.group("user")
            return SiteMatch("x", {"user": user, "id": m.group("id")})
    return None


async def site_document(url: str, *, fetch: Fetcher) -> Document | None:
    """Document from the site's API, or ``None`` (not handled / API unusable)."""
    m = match_site(url)
    if m is None:
        return None
    handler = _HANDLERS[m.site]
    try:
        doc = await handler(m, url, fetch)
    except FetchError as exc:
        log.info("%s API unavailable for %s: %s", m.site, url, exc.reason)
        return None
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        log.warning("%s API returned unexpected data for %s: %r", m.site, url, exc)
        return None
    if doc is None or not doc.blocks:
        return None
    return doc


async def _get_json(fetch: Fetcher, url: str, headers: dict[str, str] | None = None) -> Any:
    res = await fetch(url, headers={"Accept": "application/json", **(headers or {})})
    if res.status != 200 or res.blocked:
        return None
    return json.loads(res.body.decode("utf-8"))


def _iso(value: str) -> str:
    m = re.match(r"^\d{4}-\d{2}-\d{2}", value or "")
    return m.group(0) if m else ""


# --------------------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------------------

_GH_HEADERS = {"Accept": "application/vnd.github.html+json", "X-GitHub-Api-Version": "2022-11-28"}


async def _github(m: SiteMatch, url: str, fetch: Fetcher) -> Document | None:
    o, r = quote(m.params["owner"], safe=""), quote(m.params["repo"], safe="")
    ref = m.params.get("ref", "")
    if m.params["kind"] == "file":
        api = f"https://api.github.com/repos/{o}/{r}/contents/{quote(m.params['path'])}"
    else:
        sub = m.params.get("dir", "")
        api = f"https://api.github.com/repos/{o}/{r}/readme" + (f"/{quote(sub)}" if sub else "")
    if ref:
        api += f"?ref={quote(ref, safe='')}"
    res = await fetch(api, headers=_GH_HEADERS)
    if res.status != 200 or res.blocked or not res.body:
        return None
    body = res.body.decode("utf-8", "replace")
    if body.lstrip().startswith("{"):
        return None  # JSON (error or unexpected media type), not rendered HTML
    blocks = html_fragment_to_blocks(body, normalize=False)
    repo_name = f"{m.params['owner']}/{m.params['repo']}"
    title = ""
    if blocks and isinstance(blocks[0], Heading) and len(blocks[0].text) >= 2:
        title = blocks[0].text
        blocks = blocks[1:]
    blocks = normalize_heading_levels(blocks)
    if m.params["kind"] == "file":
        fname = m.params["path"].rsplit("/", 1)[-1]
        title = title or f"{repo_name}: {fname}"
    return Document(
        title=title or repo_name,
        blocks=blocks,
        byline=m.params["owner"],
        site="GitHub",
        source_url=url,
        lang=guess_lang("\n".join(getattr(b, "text", "") for b in blocks[:40])),
    )


# --------------------------------------------------------------------------------------
# Wikipedia
# --------------------------------------------------------------------------------------

# Reference-like trailing sections that are useless on an e-reader (several languages).
_WIKI_DROP = frozenset(
    {
        "references", "external links", "see also", "notes", "further reading", "bibliography",
        "sources", "citations", "footnotes", "notes and references", "works cited",
        "general and cited references", "explanatory notes", "references and notes",
        "einzelnachweise", "weblinks", "literatur", "siehe auch", "anmerkungen",
        "notes et références", "références", "liens externes", "voir aussi",
        "referencias", "enlaces externos", "véase también", "bibliografía", "notas",
        "collegamenti esterni", "voci correlate", "referências", "ligações externas", "ver também",
        "примечания", "ссылки", "литература", "см. также",
    }
)  # fmt: skip


def _wiki_head(line: str) -> tuple[int, str] | None:
    """``(level, name)`` for a stripped ``== Name ==`` line, else ``None``.

    Same result as ``re.match(r"^(={2,6})\\s*(.*?)\\s*\\1\\s*$", line)``, in linear time:
    that pattern backtracks quadratically on a line with a long inner run of spaces.
    """
    run = len(line) - len(line.lstrip("="))
    for n in range(min(run, 6), 1, -1):
        if len(line) >= 2 * n and line.endswith("=" * n):
            return n, line[n : len(line) - n].strip()
    return None


_TEX_SCAN_LIMIT = 4000  # longest {\displaystyle ...} group searched for its closing brace
_MATH_MARKER = "{\\displaystyle"

_TEX_SYMBOLS = {
    "cdot": "·", "times": "×", "pm": "±", "approx": "≈", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "neq": "≠", "ne": "≠", "to": "→", "rightarrow": "→", "leftarrow": "←",
    "infty": "∞", "ldots": "…", "dots": "…", "cdots": "…", "prime": "′", "partial": "∂",
    "sum": "Σ", "prod": "Π", "int": "∫", "propto": "∝", "equiv": "≡", "sim": "~", "circ": "°",
    "degree": "°", "quad": " ", "qquad": " ", "in": "∈", "ast": "*", "star": "*", "mid": "|",
}  # fmt: skip
_GREEK = dict(
    zip(
        "alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi "
        "pi rho sigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Phi Psi Omega".split(),
        "αβγδεεζηθθικλμνξπρστυφφχψωΓΔΘΛΞΠΣΦΨΩ",
        strict=True,
    )
)
_SUPERSCRIPT = dict(zip("123456789+-=()n", "¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ", strict=True))  # font has no U+2070 (⁰)
_SUBSCRIPT = dict(zip("0123456789+-=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ", strict=True))
_TEX_WRAPPERS = (
    "mathrm|mathbf|mathit|mathsf|mathtt|mathcal|mathbb|mathfrak|boldsymbol|vec|hat|bar|tilde|"
    "dot|ddot|overline|underline|widehat|widetilde|displaystyle"
)
_TEX_TEXT = frozenset({"text", "textrm", "textbf", "textit", "mbox", "operatorname"})  # literal words
_TEX_FUNCS = frozenset(
    "sin cos tan sec csc cot sinh cosh tanh arcsin arccos arctan log ln lg exp lim max min sup inf det dim gcd deg Pr".split()
)


def _tex_group(s: str, i: int) -> tuple[str, int]:
    """The argument starting at ``s[i]``: a ``{...}`` group or a single character."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] != "{":
        if s[i] == "\\":
            m = re.match(r"\\[A-Za-z]+", s[i:])
            if m:
                return m.group(0), i + len(m.group(0))
        return s[i], i + 1
    depth = 0
    for k in range(i, len(s)):
        if s[k] == "{":
            depth += 1
        elif s[k] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1 : k], k + 1
    return s[i + 1 :], len(s)


def _script(arg: str, table: dict[str, str], mark: str) -> str:
    plain = tex_to_text(arg)
    if plain and all(c in table for c in plain):
        return "".join(table[c] for c in plain)
    return mark + (plain if len(plain) == 1 else f"({plain})")


def tex_to_text(tex: str) -> str:
    """Readable plain text for simple TeX math (``E=mc^{2}`` -> ``E=mc²``).

    Handles fractions, roots, sub/superscripts, Greek letters, common operators and
    font wrappers; anything else loses its backslash and braces but keeps its letters.
    Spaces are dropped as in TeX math mode, except inside ``\\text{...}`` and after
    function names (``\\sin x`` -> ``sin x``).
    """
    out: list[str] = []
    i = 0
    s = tex.strip()
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)", s[i:])
            if not m:  # \, \; \! \{ \} \% ...
                nxt = s[i + 1 : i + 2]
                out.append({",": " ", ";": " ", ":": " ", "!": "", " ": " "}.get(nxt, nxt))
                i += 2
                continue
            name = m.group(1)
            i += len(m.group(0))
            if name in ("frac", "dfrac", "tfrac"):
                a, i = _tex_group(s, i)
                b, i = _tex_group(s, i)
                a, b = tex_to_text(a), tex_to_text(b)
                wrap = lambda x: x if re.fullmatch(r"[\w.′]+", x) else f"({x})"  # noqa: E731
                out.append(f"{wrap(a)}/{wrap(b)}")
            elif name == "sqrt":
                a, i = _tex_group(s, i)
                out.append(f"sqrt({tex_to_text(a)})")
            elif name in _TEX_TEXT:
                a, i = _tex_group(s, i)
                out.append(re.sub(r"[{}\\]", "", a))
            elif re.fullmatch(_TEX_WRAPPERS, name):
                if name != "displaystyle":
                    a, i = _tex_group(s, i)
                    out.append(tex_to_text(a))
            elif name in _TEX_FUNCS:
                out.append(name + " ")
            elif name in ("left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "limits"):
                continue
            elif name in _GREEK:
                out.append(_GREEK[name])
            elif name in _TEX_SYMBOLS:
                out.append(_TEX_SYMBOLS[name])
            else:
                out.append(name)
        elif c in "^_":
            arg, i = _tex_group(s, i + 1)
            if out and out[-1].endswith(" "):
                out[-1] = out[-1].rstrip()  # "\\cos^{2}x" -> "cos²x"
            out.append(_script(arg, _SUPERSCRIPT if c == "^" else _SUBSCRIPT, c))
        elif c in "{}":
            i += 1
        elif c == "~":
            out.append(" ")
            i += 1
        elif c.isspace():
            i += 1  # TeX math mode ignores spaces
        else:
            out.append(c)
            i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _balanced_end(text: str, j: int) -> int:
    """Index of the brace closing the group that opens at ``text[j]``, or -1."""
    depth = 0
    for k in range(j, min(len(text), j + _TEX_SCAN_LIMIT)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return k
    return -1


def _inline_extract_math(text: str) -> str:
    r"""Collapse the math that plain-text extracts spread over many lines.

    TextExtracts renders each formula as a run of indented one-token lines followed by
    ``{\displaystyle <TeX>}``. The run is replaced by the TeX converted to text
    (:func:`tex_to_text`) and joined back into the sentence around it. An unbalanced
    group only loses its marker, never the rest of the article.
    """
    out: list[str] = []
    i = 0
    while True:
        j = text.find(_MATH_MARKER, i)
        if j < 0:
            out.append(text[i:])
            break
        end = _balanced_end(text, j)
        if end < 0:
            out.append(text[i:j])
            i = j + len(_MATH_MARKER)
            continue
        math = tex_to_text(text[j + len(_MATH_MARKER) : end])
        line_start = max(i, text.rfind("\n", i, j) + 1)
        if text[line_start:j].strip():
            before = text[i:j]  # formula inside a prose line
        else:
            # back over the run of blank / indented token lines that precede the TeX
            start = line_start
            while start > i:
                prev_start = max(i, text.rfind("\n", i, start - 1) + 1)
                prev = text[prev_start : start - 1]
                if prev.strip() and not prev.startswith("  "):
                    break
                start = prev_start
            before = text[i:start].rstrip("\n")
        sep = "" if (not before or before.endswith((" ", "("))) else " "
        out.append(f"{before}{sep}{math}")
        nl = text.find("\n", end)
        line_end = len(text) if nl < 0 else nl
        if text[end + 1 : line_end].strip():
            i = end + 1  # prose continues on the same line
            continue
        # forward over the rest of that line and any blank lines
        k = line_end + 1
        while k < len(text):
            nl = text.find("\n", k)
            if text[k : (len(text) if nl < 0 else nl)].strip():
                break
            k = len(text) if nl < 0 else nl + 1
        if k < len(text) and text[k] not in " ,.;:)!?":
            out.append("\n")  # display formula: the next line is a new paragraph
        i = k
    return "".join(out)


def _wiki_section_dropped(name: str) -> bool:
    return name.strip().casefold() in _WIKI_DROP


def wiki_extract_to_blocks(extract: str) -> list[Block]:
    """TextExtracts plain text (``exsectionformat=wiki``) -> blocks.

    Every line is a paragraph; ``== x ==`` lines are headings. Reference-like
    sections (References, External links, See also, ...) and empty sections are dropped.
    """
    blocks: list[Block] = []
    skip_level: int | None = None
    for line in _inline_extract_math(extract or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        hm = _wiki_head(line)
        if hm:
            level = hm[0]
            name = clean_text(hm[1])
            if skip_level is not None and level > skip_level:
                continue
            skip_level = level if _wiki_section_dropped(name) else None
            if skip_level is None and name:
                blocks.append(Heading(text=name, level=level))
            continue
        if skip_level is not None:
            continue
        t = clean_text(line)
        if t:
            blocks.append(Paragraph(text=t))
    return normalize_heading_levels(drop_empty_sections(blocks))


async def _wikipedia(m: SiteMatch, url: str, fetch: Fetcher) -> Document | None:
    lang = m.params["lang"]
    title = m.params["title"]
    api = (
        f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json&formatversion=2&redirects=1"
        f"&prop=extracts%7Cinfo&inprop=url&explaintext=1&exsectionformat=wiki&titles={quote(title, safe='')}"
    )
    data = await _get_json(fetch, api)
    if not data:
        return None
    pages = (data.get("query") or {}).get("pages") or []
    if not pages:
        return None
    page = pages[0]
    if page.get("missing") or page.get("invalid") or not page.get("extract"):
        return None
    blocks = wiki_extract_to_blocks(page["extract"])
    doc_lang = "en" if lang == "simple" else re.split(r"[-_]", lang)[0]
    return Document(
        title=clean_text(page.get("title") or title),
        blocks=blocks,
        byline="",
        site="Wikipedia",
        source_url=page.get("canonicalurl") or url,
        lang=doc_lang,
    )


# --------------------------------------------------------------------------------------
# Substack
# --------------------------------------------------------------------------------------


async def _substack(m: SiteMatch, url: str, fetch: Fetcher) -> Document | None:
    api = f"{m.params['base']}/api/v1/posts/{quote(m.params['slug'], safe='')}"
    data = await _get_json(fetch, api)
    if not isinstance(data, dict) or not data.get("title"):
        return None
    blocks: list[Block] = []
    subtitle = clean_text(data.get("subtitle") or "")
    if subtitle:
        blocks.append(Paragraph(text=subtitle))
    body_html = data.get("body_html") or ""
    body_blocks = html_fragment_to_blocks(body_html, normalize=False) if body_html else []
    if not body_blocks:
        fallback = data.get("truncated_body_text") or data.get("description") or ""
        body_blocks = text_to_blocks(fallback)
    blocks.extend(body_blocks)
    bylines = data.get("publishedBylines") or []
    names = [clean_text(b.get("name") or "") for b in bylines if isinstance(b, dict)]
    site = ""
    pub_id = data.get("publication_id")
    for b in bylines:
        for pu in (b.get("publicationUsers") or []) if isinstance(b, dict) else []:
            pub = pu.get("publication") or {}
            if pub.get("id") == pub_id and pub.get("name"):
                site = clean_text(pub["name"])
                break
        if site:
            break
    notes: list[str] = []
    audience = str(data.get("audience") or "everyone")
    if audience not in ("everyone", "only_free"):
        notes.append("Subscriber-only post: only the free preview could be read.")
    lang = (data.get("language") or "").split("-")[0].lower() or guess_lang(
        "\n".join(getattr(b, "text", "") for b in blocks[:40])
    )
    return Document(
        title=clean_text(data["title"]),
        blocks=normalize_heading_levels(blocks),
        byline=clean_author(", ".join(n for n in names if n)),
        site=site or urlsplit(m.params["base"]).hostname or "Substack",
        source_url=data.get("canonical_url") or url,
        published=_iso(data.get("post_date") or ""),
        lang=lang,
        notes=notes,
    )


# --------------------------------------------------------------------------------------
# Bluesky
# --------------------------------------------------------------------------------------

_BSKY_API = "https://public.api.bsky.app/xrpc"
_BSKY_MAX_SELF_REPLIES = 50


def _bsky_post_blocks(post: dict[str, Any]) -> list[Block]:
    record = post.get("record") or {}
    blocks: list[Block] = list(text_to_blocks(record.get("text") or "", hard_wrapped=False))
    embed = post.get("embed") or {}
    blocks.extend(_bsky_embed_blocks(embed))
    return blocks


def _bsky_embed_blocks(embed: dict[str, Any]) -> list[Block]:
    kind = embed.get("$type", "")
    out: list[Block] = []
    if kind.startswith("app.bsky.embed.recordWithMedia"):
        out.extend(_bsky_embed_blocks(embed.get("media") or {}))
        out.extend(
            _bsky_embed_blocks(
                {"$type": "app.bsky.embed.record#view", "record": (embed.get("record") or {}).get("record")}
            )
        )
    elif kind.startswith("app.bsky.embed.images"):
        for img in embed.get("images") or []:
            alt = clean_text(img.get("alt") or "")
            if alt:
                out.append(Paragraph(text=f"[Image: {alt}]"))
    elif kind.startswith("app.bsky.embed.video"):
        alt = clean_text(embed.get("alt") or "")
        out.append(Paragraph(text=f"[Video: {alt}]" if alt else "[Video]"))
    elif kind.startswith("app.bsky.embed.external"):
        ext = embed.get("external") or {}
        label = clean_text(ext.get("title") or "") or clean_text(urlsplit(ext.get("uri") or "").hostname or "")
        if label:
            out.append(Paragraph(text=f"[Link: {label}]"))
    elif kind.startswith("app.bsky.embed.record"):
        rec = embed.get("record") or {}
        value = rec.get("value") or {}
        text = clean_text(value.get("text") or "")
        if text:
            author = rec.get("author") or {}
            who = clean_text(author.get("displayName") or "") or author.get("handle") or ""
            out.append(Quote(text=f"{who}: {text}" if who else text))
    return out


async def _bluesky(m: SiteMatch, url: str, fetch: Fetcher) -> Document | None:
    actor = m.params["actor"]
    if not actor.startswith("did:"):
        data = await _get_json(fetch, f"{_BSKY_API}/com.atproto.identity.resolveHandle?handle={quote(actor, safe='')}")
        if not data or not data.get("did"):
            return None
        actor = data["did"]
    uri = f"at://{actor}/app.bsky.feed.post/{m.params['rkey']}"
    data = await _get_json(
        fetch, f"{_BSKY_API}/app.bsky.feed.getPostThread?uri={quote(uri, safe='')}&depth=10&parentHeight=0"
    )
    thread = (data or {}).get("thread") or {}
    post = thread.get("post")
    if not post:
        return None  # notFoundPost / blockedPost
    author = post.get("author") or {}
    root_did = author.get("did")
    blocks = _bsky_post_blocks(post)
    # Follow the author's own continuation ("thread" of self-replies).
    node = thread
    for _ in range(_BSKY_MAX_SELF_REPLIES):
        replies = [
            r for r in node.get("replies") or [] if (r.get("post") or {}).get("author", {}).get("did") == root_did
        ]
        if not replies:
            break
        node = min(replies, key=lambda r: (r.get("post") or {}).get("indexedAt") or "")
        blocks.extend(_bsky_post_blocks(node["post"]))
    record = post.get("record") or {}
    name = clean_text(author.get("displayName") or "")
    handle = author.get("handle") or ""
    byline = f"{name} (@{handle})" if name and handle else name or (f"@{handle}" if handle else "")
    langs = record.get("langs") or []
    text = record.get("text") or ""
    return Document(
        title=derive_title(text) or f"Post by {name or handle or 'Bluesky user'}",
        blocks=blocks,
        byline=byline,
        site="Bluesky",
        source_url=url,
        published=_iso(record.get("createdAt") or ""),
        lang=(langs[0].split("-")[0].lower() if langs else guess_lang(text)),
    )


# --------------------------------------------------------------------------------------
# X / Twitter
# --------------------------------------------------------------------------------------

_PIC_RE = re.compile(r"(?:https?://)?pic\.(?:twitter|x)\.com/\S+")


def _oembed_date(text: str) -> str:
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return _dt.datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


async def _x(m: SiteMatch, url: str, fetch: Fetcher) -> Document | None:
    status_url = f"https://x.com/{m.params['user']}/status/{m.params['id']}"
    api = f"https://publish.twitter.com/oembed?url={quote(status_url, safe='')}&omit_script=1&dnt=true"
    data = await _get_json(fetch, api)
    if not isinstance(data, dict) or not data.get("html"):
        return None
    root = lxml_html.fragment_fromstring(data["html"], create_parent="div")
    bq = next(root.iter("blockquote"), None)
    if bq is None:
        return None
    blocks: list[Block] = []
    lang = ""
    for p in bq.iter("p"):
        lang = lang or (p.get("lang") or "")
        for br in p.iter("br"):
            br.tail = "\n" + (br.tail or "")
        text = _PIC_RE.sub("", "".join(p.itertext()))
        blocks.extend(text_to_blocks(text, hard_wrapped=False))
    links = list(bq.iter("a"))
    published = _oembed_date(links[-1].text_content()) if links else ""
    author = clean_text(data.get("author_name") or "")
    handle = (data.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1]
    byline = f"{author} (@{handle})" if author and handle and handle != author else author
    plain = "\n".join(getattr(b, "text", "") for b in blocks)
    if lang in ("und", "zxx", "qme", "qht", "qam", "art"):
        lang = ""
    return Document(
        title=derive_title(plain) or f"Post by {author or 'X user'}",
        blocks=blocks,
        byline=byline,
        site="X",
        source_url=url,
        published=published,
        lang=lang.split("-")[0].lower() if lang else guess_lang(plain),
    )


_HANDLERS: dict[str, _Handler] = {
    "github": _github,
    "wikipedia": _wikipedia,
    "substack": _substack,
    "bluesky": _bluesky,
    "x": _x,
}
