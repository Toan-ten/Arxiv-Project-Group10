import re
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET


NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}
ARXIV_HOSTS = {"arxiv.org", "export.arxiv.org"}
ID_PATTERN = re.compile(
    r"(?P<base>[0-9]{2}(?:0[1-9]|1[0-2])\.[0-9]{4,5}"
    r"|[A-Za-z][A-Za-z0-9.-]*/[0-9]{2}(?:0[1-9]|1[0-2])[0-9]{3})"
    r"(?:v(?P<version>[1-9][0-9]*))?"
)


def _text(element: ET.Element, path: str) -> str | None:
    nodes = element.findall(path, NS)
    if len(nodes) > 1:
        raise ValueError(f"Repeated field: {path}")
    if not nodes:
        return None
    return "".join(nodes[0].itertext()).strip() or None


def _entry_id(value: str) -> tuple[str, int | None]:
    url = urlsplit(value)
    if (url.scheme not in {"http", "https"} or url.netloc not in ARXIV_HOSTS
            or not url.path.startswith("/abs/") or url.query or url.fragment):
        raise ValueError(f"Invalid arXiv entry URL: {value}")
    match = ID_PATTERN.fullmatch(url.path.removeprefix("/abs/"))
    if match is None:
        raise ValueError(f"Invalid arXiv entry ID: {value}")
    version = match.group("version")
    return match.group("base"), int(version) if version is not None else None


def parse_arxiv_response(xml: str | bytes, mssv: str, arxiv_id: str) -> dict:
    if not isinstance(mssv, str) or not re.fullmatch(r"[0-9]+", mssv):
        raise ValueError("mssv must be a non-empty string of digits")
    requested = ID_PATTERN.fullmatch(arxiv_id) if isinstance(arxiv_id, str) else None
    if requested is None or requested.group("version") is not None:
        raise ValueError("arxiv_id must be a base ID without a version suffix")
    if not isinstance(xml, (str, bytes)):
        raise TypeError("xml must be str or bytes")

    result = {
        "mssv": mssv,
        "arxiv_id": arxiv_id,
        "status": "invalid_response",
        "latest_version_id": None,
        "version_ids": [],
        "version_status": "not_applicable",
        "title": None,
        "authors": [],
        "published": None,
        "updated": None,
        "missing_fields": [],
        "error": None,
    }
    try:
        feed = ET.fromstring(xml)
    except (ET.ParseError, ValueError, LookupError) as exc:
        result.update(status="invalid_xml", error=str(exc))
        return result

    try:
        if feed.tag != f"{{{NS['atom']}}}feed":
            raise ValueError("Expected an Atom feed with the Atom namespace")
        entries = feed.findall("atom:entry", NS)
        if len(entries) > 1:
            raise ValueError("Expected at most one entry for a single base-ID lookup")
        for node in feed.iter():
            if node.tag.rsplit("}", 1)[-1] == "entry" and node not in entries:
                raise ValueError("Unexpected entry namespace or nested entry")
        for link in feed.findall("atom:link", NS):
            if link.get("rel") == "self" and link.get("href"):
                query = parse_qs(urlsplit(link.get("href")).query, keep_blank_values=True)
                if "id_list" in query and query["id_list"] != [arxiv_id]:
                    raise ValueError("Feed self link does not match the requested base ID")

        counts = {}
        for name in ("totalResults", "startIndex"):
            path = f"opensearch:{name}"
            if feed.find(path, NS) is not None:
                value = _text(feed, path)
                if value is None or not re.fullmatch(r"[0-9]+", value):
                    raise ValueError(f"Invalid OpenSearch {name}")
                counts[name] = int(value)
        if counts.get("startIndex", 0) != 0:
            raise ValueError("Expected the first page of a base-ID lookup")
        if not entries:
            if counts.get("totalResults") != 0:
                raise ValueError("Empty feed must explicitly report totalResults=0")
            result["status"] = "not_found"
            return result
        if counts.get("totalResults", 1) != 1:
            raise ValueError("totalResults does not match a single-entry response")

        entry = entries[0]
        identifier = _text(entry, "atom:id")
        if identifier is None:
            raise ValueError("Entry is missing its arXiv ID")
        url = urlsplit(identifier)
        if (url.scheme in {"http", "https"} and url.netloc in ARXIV_HOSTS
                and url.path == "/api/errors"):
            message = _text(entry, "atom:summary") or "arXiv API error entry"
            result.update(status="api_error", error=f"{identifier}: {message}")
            return result

        base_id, version = _entry_id(identifier)
        if base_id != arxiv_id:
            raise ValueError(f"Requested {arxiv_id}, received {base_id}")
        versions = set() if version is None else {version}
        for link in entry.findall("atom:link", NS):
            href = link.get("href")
            if link.get("rel") != "alternate" or not href:
                continue
            url = urlsplit(href)
            if url.netloc not in ARXIV_HOSTS or not url.path.startswith("/abs/"):
                continue
            linked_id, linked_version = _entry_id(href)
            if linked_id != arxiv_id:
                raise ValueError(f"Alternate link points to a different ID: {linked_id}")
            if linked_version is not None:
                versions.add(linked_version)
        if len(versions) > 1:
            raise ValueError("Conflicting version identifiers in entry and alternate link")

        title = _text(entry, "atom:title")
        title = " ".join(title.split()) if title else None
        authors = []
        missing = []
        for index, author in enumerate(entry.findall("atom:author", NS)):
            name = _text(author, "atom:name")
            if name is None:
                missing.append(f"authors[{index}].name")
            else:
                authors.append(" ".join(name.split()))
        published = _text(entry, "atom:published")
        updated = _text(entry, "atom:updated")
        for field, value in (("title", title), ("authors", authors),
                             ("published", published), ("updated", updated)):
            if not value:
                missing.append(field)
        result.update(
            status="found", title=title, authors=authors, published=published,
            updated=updated, missing_fields=missing, version_status="needs_verification",
        )
        if versions:
            latest = versions.pop()
            result.update(
                latest_version_id=f"{base_id}v{latest}",
                version_ids=[f"{base_id}v{number}" for number in range(1, latest + 1)],
                version_status="resolved",
            )
    except ValueError as exc:
        result.update(status="invalid_response", error=str(exc))
    return result
