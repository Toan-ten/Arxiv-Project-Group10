"""Version-specific checks of official arXiv abstract pages."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import re
from urllib.parse import quote, urlsplit

from src import arxiv_client
from src.arxiv_discovery import ID_PATTERN

MAX_PAGE_BYTES = 2 * 1024 * 1024
_WITHDRAWN_NOTICE = re.compile(
    r"\b(?:this paper has been withdrawn|this version has been withdrawn|"
    r"this paper was withdrawn|this version was withdrawn)\b",
    re.IGNORECASE,
)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def verify_version_withdrawal(arxiv_id: str, version: str, *, timeout=30.0,
                              max_attempts=3, user_agent=arxiv_client.USER_AGENT) -> dict:
    """Fetch and inspect the exact official version page through the shared limiter."""
    base = ID_PATTERN.fullmatch(arxiv_id) if isinstance(arxiv_id, str) else None
    if base is None or base.group("version") is not None:
        raise ValueError("arxiv_id must be a base arXiv ID")
    if not isinstance(version, str) or not re.fullmatch(r"v[1-9][0-9]*", version):
        raise ValueError("version must be a canonical vN name")
    target = arxiv_id + version
    url = "https://arxiv.org/abs/" + quote(target, safe="/")

    def read_page(response):
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "text/html":
            return {"status": "invalid_response", "error": "Expected official HTML page"}
        raw = response.read(MAX_PAGE_BYTES + 1)
        if len(raw) > MAX_PAGE_BYTES:
            return {"status": "invalid_response", "error": "Official page exceeds size limit"}
        try:
            html = raw.decode("utf-8")
            parser = _VisibleText()
            parser.feed(html)
            parser.close()
        except (UnicodeError, ValueError):
            return {"status": "invalid_response", "error": "Official page is not valid UTF-8 HTML"}
        final_url = response.geturl() if callable(getattr(response, "geturl", None)) else url
        parsed = urlsplit(final_url)
        if (parsed.scheme != "https" or parsed.netloc != "arxiv.org"
                or parsed.path != urlsplit(url).path or parsed.query or parsed.fragment):
            return {"status": "invalid_response", "error": "Official page redirected to a different identity"}
        text = " ".join(" ".join(parser.parts).split())
        if target not in text:
            return {"status": "invalid_response", "error": "Official page does not identify the requested version"}
        match = _WITHDRAWN_NOTICE.search(text)
        if not match:
            return {"status": "not_withdrawn", "error": None}
        excerpt = text[match.start():min(len(text), match.end() + 120)]
        return {
            "status": "confirmed_withdrawn",
            "arxiv_id": arxiv_id,
            "versioned_id": target,
            "official_url": url,
            "withdrawal_reason": excerpt,
            "page_sha256": sha256(raw).hexdigest(),
            "verification_method": "official_arxiv_version_page",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }

    result = arxiv_client._request_arxiv(
        url, timeout=timeout, max_attempts=max_attempts, user_agent=user_agent,
        accept="text/html", read_response=read_page,
    )
    return {"url": url, **result}
