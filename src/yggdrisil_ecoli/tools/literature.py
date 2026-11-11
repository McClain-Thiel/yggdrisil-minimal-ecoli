"""Read-only Europe PMC retrieval for the ordinary-search experiment arm."""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from yggdrisil.agents import ToolInputError

_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
logger = logging.getLogger(__name__)


def _cutoff() -> str | None:
    value = os.environ.get("YGGDRISIL_LITERATURE_CUTOFF")
    if value is not None:
        date.fromisoformat(value)
        if not os.environ.get("YGGDRISIL_LITERATURE_ARCHIVE"):
            raise RuntimeError(
                "A literature cutoff requires a durable response archive"
            )
    return value


def _archive(url: str, content: bytes) -> None:
    folder = os.environ.get("YGGDRISIL_LITERATURE_ARCHIVE")
    if not folder:
        return
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    path = root / (digest + ".response")
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(
                "Literature response digest collision or corrupt archive"
            )
    else:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    with (root / "requests.jsonl").open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "url": url,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "sha256": digest,
                    "bytes": len(content),
                    "cutoff": _cutoff(),
                }
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def _search_url(query: str, count: int) -> str:
    cutoff = _cutoff()
    if cutoff:
        query = f"({query}) AND FIRST_PDATE:[1000-01-01 TO {cutoff}]"
    return f"{_BASE}/search?" + urlencode(
        {
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": count,
        }
    )


def _check_dates(papers: list[dict[str, object]]) -> None:
    cutoff = _cutoff()
    if cutoff:
        for paper in papers:
            value = paper.get("firstPublicationDate")
            if not isinstance(value, str) or date.fromisoformat(
                value
            ) > date.fromisoformat(cutoff):
                raise RuntimeError(
                    "Europe PMC returned missing or post-cutoff publication date"
                )


def _get(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "yggdrisil-ecoli/0.1"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                content: bytes = response.read()
                _archive(url, content)
                return content
        except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
            transient = (
                (isinstance(exc, HTTPError) and exc.code in {429, 500, 502, 503, 504})
                or isinstance(exc, (TimeoutError, ConnectionError))
                or (
                    isinstance(exc, URLError)
                    and isinstance(exc.reason, (TimeoutError, ConnectionError))
                )
            )
            if not transient or attempt == 2:
                raise
            logger.warning(
                "Europe PMC %s; retrying identical request (%s/2): %s",
                f"HTTP {exc.code}"
                if isinstance(exc, HTTPError)
                else type(exc).__name__,
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
    url = _search_url(query, count)
    payload = json.loads(await asyncio.to_thread(_get, url))
    _check_dates(payload["resultList"]["result"])
    fields = (
        "id",
        "source",
        "pmcid",
        "doi",
        "title",
        "authorString",
        "pubYear",
        "abstractText",
        "firstPublicationDate",
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
    if _cutoff():
        # A direct identifier must not bypass the same information boundary used
        # by search. Check metadata before fetching or exposing the full text.
        payload = json.loads(
            await asyncio.to_thread(
                _get, _search_url(f"EXT_ID:{pmcid} AND SRC:PMC", 10)
            )
        )
        papers = payload["resultList"]["result"]
        _check_dates(papers)
        if not any(p.get("pmcid") == pmcid or p.get("id") == pmcid for p in papers):
            raise ToolInputError(
                "Full text is outside the frozen publication cutoff or lacks dated metadata"
            )
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
