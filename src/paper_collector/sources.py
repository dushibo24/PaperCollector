from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

from paper_collector.config import AppConfig, Interest
from paper_collector.models import Author, Paper

LOGGER = logging.getLogger(__name__)


class SourceError(RuntimeError):
    pass


def _request_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PaperCollector/0.1 (research tool)",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SourceError(f"请求失败 {url}: {exc}") from exc


def _abstract_from_inverted(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    tokens: list[tuple[int, str]] = []
    for word, positions in index.items():
        tokens.extend((position, word) for position in positions)
    return " ".join(word for _, word in sorted(tokens))


def _stable_id(title: str, doi: str | None = None) -> str:
    if doi:
        return "doi:" + doi.casefold().removeprefix("https://doi.org/")
    normalized = re.sub(r"\W+", " ", title.casefold()).strip()
    return "title:" + hashlib.sha256(normalized.encode()).hexdigest()[:24]


class OpenAlexSource:
    base_url = "https://api.openalex.org"

    def __init__(self, config: AppConfig):
        self.config = config

    def discover(self, interest: Interest) -> list[Paper]:
        today = datetime.now(UTC).date()
        from_recent = today - timedelta(days=self.config.recent_days)
        from_evergreen = date(today.year - self.config.evergreen_years, 1, 1)
        query = " ".join(interest.keywords[:6]) or interest.description
        common = {
            "search": query,
            "per-page": str(self.config.per_interest_limit),
            "mailto": self.config.email,
            "select": "id,doi,title,display_name,publication_date,publication_year,authorships,primary_location,best_oa_location,cited_by_count,abstract_inverted_index,concepts,type",
        }
        papers: dict[str, Paper] = {}
        requests = [
            (from_recent, "publication_date:desc"),
            (from_evergreen, "cited_by_count:desc"),
        ]
        for from_date, sort in requests:
            params = dict(common)
            params["filter"] = (
                f"from_publication_date:{from_date.isoformat()},to_publication_date:{today.isoformat()},has_abstract:true"
            )
            params["sort"] = sort
            url = f"{self.base_url}/works?{urllib.parse.urlencode(params)}"
            payload = _request_json(url)
            for raw in payload.get("results", []):
                paper = self._convert(raw, interest.name)
                if paper.abstract:
                    papers[paper.id] = paper
        return list(papers.values())

    def _convert(self, raw: dict[str, Any], interest_name: str) -> Paper:
        current_year = datetime.now(UTC).year
        doi = raw.get("doi")
        title = raw.get("display_name") or raw.get("title") or "Untitled"
        primary = raw.get("primary_location") or {}
        best_oa = raw.get("best_oa_location") or {}
        source = primary.get("source") or {}
        authors = [
            Author(
                name=(item.get("author") or {}).get("display_name", "Unknown"),
                openalex_id=(item.get("author") or {}).get("id"),
            )
            for item in raw.get("authorships", [])
        ]
        concepts = [
            item.get("display_name", "")
            for item in raw.get("concepts", [])[:12]
            if item.get("score", 0) >= 0.3
        ]
        return Paper(
            id=_stable_id(title, doi),
            doi=doi.removeprefix("https://doi.org/") if doi else None,
            title=title,
            abstract=_abstract_from_inverted(raw.get("abstract_inverted_index")),
            authors=authors,
            publication_date=raw.get("publication_date")
            or f"{raw.get('publication_year', current_year)}-01-01",
            year=int(raw.get("publication_year") or current_year),
            venue=source.get("display_name") or "",
            venue_type=source.get("type") or raw.get("type") or "",
            landing_url=best_oa.get("landing_page_url")
            or primary.get("landing_page_url")
            or raw.get("id"),
            pdf_url=best_oa.get("pdf_url") or primary.get("pdf_url"),
            cited_by_count=int(raw.get("cited_by_count") or 0),
            source="openalex",
            concepts=concepts,
            matched_interests=[interest_name],
        )

    def enrich_authors(self, paper: Paper, max_authors: int = 4) -> Paper:
        for author in paper.authors[:max_authors]:
            if not author.openalex_id:
                continue
            author_id = author.openalex_id.rsplit("/", 1)[-1]
            params = urllib.parse.urlencode({"mailto": self.config.email})
            try:
                raw = _request_json(f"{self.base_url}/authors/{author_id}?{params}")
            except SourceError:
                continue
            author.cited_by_count = int(raw.get("cited_by_count") or 0)
            author.h_index = int((raw.get("summary_stats") or {}).get("h_index") or 0)
            counts = raw.get("counts_by_year") or []
            author.notable_work_count = sum(
                int(item.get("works_count") or 0) for item in counts[:5]
            )
            time.sleep(0.05)
        return paper


class ArxivSource:
    base_url = "https://export.arxiv.org/api/query"
    ns: ClassVar[dict[str, str]] = {
        "a": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }

    def __init__(self, config: AppConfig):
        self.config = config

    def discover(self, interest: Interest) -> list[Paper]:
        terms = interest.keywords[:4] or [interest.description]
        search_query = " OR ".join(f'all:"{term}"' for term in terms)
        params = urllib.parse.urlencode(
            {
                "search_query": search_query,
                "start": 0,
                "max_results": self.config.per_interest_limit,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        request = urllib.request.Request(
            f"{self.base_url}?{params}",
            headers={"User-Agent": f"PaperCollector/0.1 ({self.config.email})"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                root = ET.parse(response).getroot()
        except (urllib.error.URLError, ET.ParseError, TimeoutError) as exc:
            raise SourceError(f"arXiv 请求失败: {exc}") from exc
        papers: list[Paper] = []
        today = datetime.now(UTC).date()
        cutoff = today - timedelta(days=self.config.recent_days)
        for entry in root.findall("a:entry", self.ns):
            published = (entry.findtext("a:published", default="", namespaces=self.ns) or "")[:10]
            try:
                if date.fromisoformat(published) < cutoff:
                    continue
            except ValueError:
                pass
            landing = entry.findtext("a:id", default="", namespaces=self.ns)
            arxiv_id = landing.rsplit("/", 1)[-1]
            title = " ".join(
                entry.findtext("a:title", default="Untitled", namespaces=self.ns).split()
            )
            abstract = " ".join(entry.findtext("a:summary", default="", namespaces=self.ns).split())
            authors = [
                Author(name=node.findtext("a:name", default="Unknown", namespaces=self.ns))
                for node in entry.findall("a:author", self.ns)
            ]
            doi = entry.findtext("arxiv:doi", default=None, namespaces=self.ns)
            papers.append(
                Paper(
                    id=_stable_id(title, doi) if doi else f"arxiv:{arxiv_id}",
                    doi=doi,
                    title=title,
                    abstract=abstract,
                    authors=authors,
                    publication_date=published,
                    year=int(published[:4]) if published else today.year,
                    venue="arXiv",
                    venue_type="preprint",
                    landing_url=landing,
                    pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                    source="arxiv",
                    matched_interests=[interest.name],
                )
            )
        return papers


class SemanticScholarEnricher:
    base_url = "https://api.semanticscholar.org/graph/v1/paper"
    min_request_interval_seconds = 1.0
    default_daily_request_limit = 30

    def __init__(
        self,
        api_key: str | None,
        *,
        daily_request_limit: int = default_daily_request_limit,
        reserve_request: Callable[[], bool] | None = None,
    ):
        self.api_key = api_key
        self.daily_request_limit = daily_request_limit
        self._reserve_request = reserve_request
        self._quota_date: date | None = None
        self._requests_today = 0
        self._quota_warning_emitted = False
        self._last_request_started_at: float | None = None

    def _reserve_daily_quota(self) -> bool:
        if self._reserve_request is not None:
            return self._reserve_request()
        today = datetime.now().astimezone().date()
        if today != self._quota_date:
            self._quota_date = today
            self._requests_today = 0
        if self._requests_today >= self.daily_request_limit:
            return False
        self._requests_today += 1
        return True

    def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_request_started_at is not None:
            elapsed = now - self._last_request_started_at
            delay = self.min_request_interval_seconds - elapsed
            if delay > 0:
                time.sleep(delay)
                now += delay
        self._last_request_started_at = now

    def enrich(self, paper: Paper) -> Paper:
        identifier = f"DOI:{paper.doi}" if paper.doi else paper.id.replace("arxiv:", "ARXIV:")
        if identifier.startswith("title:"):
            return paper
        url = f"{self.base_url}/{urllib.parse.quote(identifier, safe=':')}?fields=citationCount,influentialCitationCount,openAccessPdf,url"
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        if not self._reserve_daily_quota():
            if not self._quota_warning_emitted:
                LOGGER.warning(
                    "Semantic Scholar 当日请求额度已用尽（上限 %s），跳过后续补充",
                    self.daily_request_limit,
                )
                self._quota_warning_emitted = True
            return paper
        self._quota_warning_emitted = False
        self._wait_for_rate_limit()
        try:
            raw = _request_json(url, headers=headers)
        except SourceError:
            return paper
        paper.cited_by_count = max(paper.cited_by_count, int(raw.get("citationCount") or 0))
        paper.influential_citation_count = int(raw.get("influentialCitationCount") or 0)
        oa = raw.get("openAccessPdf") or {}
        paper.pdf_url = paper.pdf_url or oa.get("url")
        paper.landing_url = paper.landing_url or raw.get("url")
        return paper
