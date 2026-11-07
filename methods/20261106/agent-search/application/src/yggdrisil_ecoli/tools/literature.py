"""Read-only Europe PMC retrieval for the ordinary-search experiment arm."""

import asyncio
import json
import logging
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from yggdrisil.agents import ToolInputError

_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
logger = logging.getLogger(__name__)


def _get(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "yggdrisil-ecoli/0.1"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                content: bytes = response.read()
                return content
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            logger.warning(
                "Europe PMC HTTP %s; retrying request (%s/2): %s",
                exc.code,
                attempt + 1,
                url,
            )
            time.sleep(attempt + 1)
    raise AssertionError("unreachable")


async def search_literature(query: str, count: int = 5) -> dict[str, object]:
    """Search papers with Europe PMC AND/OR queries and quoted phrases.

    Return 1–10 cited results with available abstracts; check relevance before use.
    """
    if not query.strip() or not 1 <= count <= 10:
        raise ToolInputError("query must be nonempty and count must be 1 to 10")
    url = f"{_BASE}/search?" + urlencode(
        {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": count,
        }
    )
    payload = json.loads(await asyncio.to_thread(_get, url))
    fields = (
        "id",
        "source",
        "pmcid",
        "doi",
        "title",
        "authorString",
        "pubYear",
        "abstractText",
    )
    return {
        "query": query,
        "source_url": url,
        "hit_count": payload["hitCount"],
        "papers": [
            {key: paper[key] for key in fields if key in paper}
            for paper in payload["resultList"]["result"]
        ],
    }


async def read_literature(
    pmcid: str,
    offset: int = 0,
    count: int = 12000,
) -> dict[str, object]:
    """Read a page of accessible full text; unavailable articles raise an HTTP error."""
    if not pmcid.startswith("PMC") or not pmcid[3:].isdigit():
        raise ToolInputError("pmcid must be a PMC identifier such as PMC6389913")
    if offset < 0 or not 1 <= count <= 20000:
        raise ToolInputError("offset must be non-negative and count must be 1 to 20000")
    url = f"{_BASE}/{pmcid}/fullTextXML"
    root = ElementTree.fromstring(await asyncio.to_thread(_get, url))
    text = " ".join(part.strip() for part in root.itertext() if part.strip())
    return {
        "pmcid": pmcid,
        "source_url": url,
        "total_chars": len(text),
        "offset": offset,
        "text": text[offset : offset + count],
    }
